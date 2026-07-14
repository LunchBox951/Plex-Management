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
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = [
    "TERMINAL_REQUEST_STATUS_VALUES",
    "CreateRequestResult",
    "MediaNotFoundError",
    "MediaTypeDeferredError",
    "NoAiredSeasonsError",
    "count_subscribers",
    "create_request",
    "create_request_result",
    "fold_requests_for_display",
    "get_request",
    "is_request_visible_to_user",
    "list_requests",
    "list_requests_for_user",
    "list_subscribed_request_ids",
    "list_subscribers",
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

# Statuses from which a park to ``no_acceptable_release`` is a SAFE, honest
# search-exhausted verdict -- the CAS's ``allowed_from`` for
# :func:`mark_no_acceptable_release` below (issue #72). This is deliberately
# NARROWER than "every non-terminal status": every TERMINAL status above is
# excluded (the never-un-terminate guard: writing ``no_acceptable_release`` --
# itself non-terminal and dedup-blocking -- over a finished request would
# resurrect it as a ghost that re-blocks a fresh request for the same media),
# PLUS two additional non-terminal statuses a parking transition must never
# stomp:
#   - ``downloading`` -- a grab genuinely landed on this request. Before this
#     CAS existed, ``mark_no_acceptable_release`` read the current status,
#     checked it was non-terminal, and then wrote unconditionally; a
#     concurrent writer (a lower-ranked auto-grab candidate, a manual
#     re-grab) moving the row to ``downloading`` in the gap between that read
#     and the write would be silently regressed back to a dead-end, even
#     though a real download was now live. The CAS closes this exact TOCTOU:
#     the UPDATE's ``WHERE status IN (...)`` is evaluated by the DATABASE at
#     write time, never against a stale in-memory read.
#   - ``import_blocked`` -- a DIFFERENT needs-attention dead-end (the download
#     finished but import failed). Parking over it would hide a real,
#     already-failed import behind the honest-but-now-WRONG "nothing found"
#     verdict.
# ``partially_available`` never occurs on a MOVIE's own row (it is the
# TV-only parent rollup -- see its member comment on ``RequestStatus``), so
# its absence here is moot for this module;
# ``season_request_service._PARKABLE_SEASON_STATUS_VALUES`` is the
# season-granularity analogue, whose row CAN reach ``import_blocked``
# directly.
_PARKABLE_REQUEST_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value
    for s in (
        RequestStatus.pending,
        RequestStatus.searching,
        RequestStatus.no_acceptable_release,
    )
)

# String-valued mirror of ``repositories.requests._SETTLED_REQUEST_STATUSES``,
# for the requests-list fold (:func:`fold_requests_for_display`) -- that helper
# works over ``RequestRecord.status`` (already a ``str``, per the DTO), so it
# needs the string-valued set rather than the SQL-side ``RequestStatus`` enum
# frozenset. ``completed`` and ``no_acceptable_release`` are deliberately NOT
# settled here, for the SAME reason the repo module documents: a ``completed``
# ("Finalizing") row must keep shadowing a stale duplicate rather than being
# folded away by one. Kept in sync with the repo module's set by
# ``test_settled_status_value_sets_in_sync`` -- if the two ever drift, that test
# fails loudly rather than the list quietly folding the wrong row.
_SETTLED_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value
    for s in (
        RequestStatus.available,
        RequestStatus.failed,
        RequestStatus.evicted,
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


class MediaTypeDeferredError(Exception):
    """The app cannot safely process this media type yet."""

    def __init__(self, media_type: str) -> None:
        self.media_type = media_type
        super().__init__(f"{media_type} requests are deferred")


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


class CreateRequestResult(NamedTuple):
    """The returned request plus whether this call created the returned row."""

    record: RequestRecord
    created: bool


class _TvSeasonPlan(NamedTuple):
    """Resolved TV season rows plus any explicit unaired seasons to park as waiting."""

    season_numbers: list[int]
    waiting_seasons: set[int]
    episode_scoped: bool = False


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


def _tv_request_intent(
    seasons: list[int] | None,
    episodes: dict[int, list[int]] | None = None,
) -> tuple[str, list[int] | None, dict[int, tuple[int, ...]] | None]:
    if episodes:
        return (
            "explicit_episodes",
            sorted(set(episodes)),
            {season: tuple(sorted(set(values))) for season, values in episodes.items()},
        )
    if seasons:
        return "explicit_seasons", sorted(set(seasons)), None
    return "whole_show", None, None


async def _merge_tv_request_intent(
    repo: SqlRequestRepository,
    record: RequestRecord,
    seasons: list[int] | None,
    episodes: dict[int, list[int]] | None = None,
) -> None:
    incoming_mode, incoming_requested, incoming_episodes = _tv_request_intent(seasons, episodes)
    if record.tv_request_mode == "whole_show":
        return
    merged_requested = set(record.requested_seasons or ()) | set(record.requested_episodes or {})
    merged_episodes: dict[int, set[int]] = {
        season: set(values) for season, values in (record.requested_episodes or {}).items()
    }
    if incoming_mode == "explicit_episodes":
        for season, values in (incoming_episodes or {}).items():
            whole_season_already_requested = (
                season in merged_requested and season not in merged_episodes
            )
            merged_requested.add(season)
            # A whole-season request for this season already dominates any later
            # episode subset. Preserve the broader intent instead of narrowing it.
            if whole_season_already_requested:
                continue
            merged_episodes.setdefault(season, set()).update(values)
        mode = "explicit_episodes" if merged_episodes else "explicit_seasons"
        await repo.set_tv_request_intent(
            record.id,
            mode=mode,
            requested_seasons=sorted(merged_requested),
            requested_episodes=(
                {season: tuple(sorted(values)) for season, values in merged_episodes.items()}
                if merged_episodes
                else None
            ),
        )
        return
    if incoming_mode == "whole_show":
        await repo.set_tv_request_intent(
            record.id, mode="whole_show", requested_seasons=None, requested_episodes=None
        )
        return
    merged_requested.update(incoming_requested or ())
    for whole_season in incoming_requested or ():
        # A later explicit whole-season request widens a prior episode-filtered
        # request for the same season.
        merged_episodes.pop(whole_season, None)
    mode = "explicit_episodes" if merged_episodes else "explicit_seasons"
    await repo.set_tv_request_intent(
        record.id,
        mode=mode,
        requested_seasons=sorted(merged_requested),
        requested_episodes=(
            {season: tuple(sorted(values)) for season, values in merged_episodes.items()}
            if merged_episodes
            else None
        ),
    )


async def _resolve_tv_season_plan(
    tmdb: MetadataPort,
    tmdb_id: int,
    *,
    seasons: list[int] | None,
    episodes: dict[int, list[int]] | None,
    detail: _Detail | None = None,
    default_waiting_first_season: bool = False,
) -> _TvSeasonPlan:
    """Resolve TV season rows and explicit unaired seasons consistently.

    For call sites that never resolved a ``_Detail`` (the dedup / integrity-race
    paths below may skip that TMDB round-trip), this fetches the show metadata so
    explicit future seasons can be parked as ``waiting_for_air_date`` the same way
    the fresh-create path does.
    """
    if detail is None:
        tv = await tmdb.get_tv_show(tmdb_id)
        if tv is None:
            raise MediaNotFoundError(tmdb_id, "tv")
        detail = _Detail(
            tv.title, tv.year, tv.is_anime, tv.poster_url, tv.backdrop_url, tv.season_count
        )

    if episodes:
        season_numbers = sorted(set(episodes))
    else:
        season_numbers = _season_numbers(seasons, detail.season_count)

    waiting_seasons: set[int] = set()
    if not season_numbers and default_waiting_first_season:
        season_numbers = [1]
        waiting_seasons.add(1)
    elif seasons or episodes:
        waiting_seasons.update(season for season in season_numbers if season > detail.season_count)
    return _TvSeasonPlan(
        season_numbers=season_numbers,
        waiting_seasons=waiting_seasons,
        episode_scoped=bool(episodes),
    )


async def _ensure_tv_season_plan(
    session: AsyncSession,
    library: LibraryPort | None,
    *,
    media_request_id: int,
    tmdb_id: int,
    plan: _TvSeasonPlan,
) -> None:
    await season_request_service.ensure_seasons(
        session,
        library,
        media_request_id=media_request_id,
        tmdb_id=tmdb_id,
        seasons=plan.season_numbers,
        force_pending=plan.episode_scoped,
    )
    for waiting_season in sorted(plan.waiting_seasons):
        await season_request_service.set_status(
            session,
            media_request_id=media_request_id,
            season_number=waiting_season,
            status=RequestStatus.waiting_for_air_date.value,
        )


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


def _owned_by_another_user(
    record: RequestRecord, user_id: int | None, actor_is_admin: bool
) -> bool:
    """Whether returning/mutating ``record`` for this actor crosses user ownership.

    True only for a NON-ADMIN authenticated user (``user_id`` set,
    ``actor_is_admin`` False) hitting a row owned by a DIFFERENT user. Admins and
    API-key automation (``user_id`` None) are never blocked — they can already see
    every request — and an ownerless row is subscribable, not foreign. The ONE shared
    predicate for every dedup path in this module (the initial ``find_active``
    dedup, the terminal ``find_in_library`` short-circuits, the ``IntegrityError``
    race recovery, and the available-race collapse), so no path can drift into
    silently returning or mutating another user's request. Full shared-ownership
    modeling is tracked in issue #58.
    """
    return (
        user_id is not None
        and not actor_is_admin
        and record.user_id is not None
        and record.user_id != user_id
    )


def _dedup_preference_user_id(user_id: int | None, actor_is_admin: bool) -> int | None:
    """The ``prefer_user_id`` scope for a terminal in-library dedup lookup.

    Non-admin authenticated users get the owner-preference lookup (their own terminal row,
    then an ownerless subscribable one, before a foreign row; see
    :meth:`~plex_manager.ports.repositories.RequestRepository.find_in_library`).
    Without it, multiple terminal rows for one media (a legitimate state — the
    remove-then-reacquire flow, or the per-user keep-own-row race collapse) make
    the dedup check only the newest GLOBAL row: user A, who owns an older visible
    ``available`` row, would otherwise be collapsed onto user B's newer row even
    though their own row is the more precise match. Admins and API-key automation
    (``user_id`` ``None``) keep the unscoped newest-row-wins lookup unchanged.
    """
    return user_id if user_id is not None and not actor_is_admin else None


async def _subscribe_dedup_winner(
    session: AsyncSession,
    repo: SqlRequestRepository,
    record: RequestRecord,
    user_id: int | None,
) -> RequestRecord:
    """Subscribe a signed-in requester to an ownerless dedup winner.

    Ownerless rows are automation provenance, not unclaimed destructive authority:
    the first browser user to name the same TMDB id must not gain cancel/report
    rights over an API-key-created download. Subscriber membership keeps the dedup
    result visible while ``user_id`` remains ``None``. The post-commit collapse of
    a user's own newly-created loser is the separate, narrow ownership-transfer
    case handled by :func:`_collapse_available_race`.
    """
    if user_id is None:
        return record
    await repo.add_subscriber(record.id, user_id)
    await session.commit()
    return await repo.get_fresh(record.id) or record


async def _subscribe_user(
    session: AsyncSession,
    repo: SqlRequestRepository,
    record: RequestRecord,
    user_id: int | None,
) -> RequestRecord:
    """Persist shared visibility for an authenticated requester.

    Withdrawal + collaborative cancellation shipped in issue #314
    (``correction_service.withdraw_participant`` / ``cancel_request_as_owner``,
    ``DELETE /api/v1/requests/{id}/subscription``). TODO(issue #314): per-user
    TV season-scope intent (differing season sets under one shared request)
    remains deferred -- this function still just grants VIEW-and-withdraw
    visibility, never distinct per-subscriber season intent.
    """
    if user_id is None:
        return record
    await repo.add_subscriber(record.id, user_id)
    await session.commit()
    return record


async def _collapse_available_race(
    session: AsyncSession,
    repo: SqlRequestRepository,
    record: RequestRecord,
    tmdb_id: int,
    media_type: str,
    *,
    user_id: int | None = None,
    actor_is_admin: bool = False,
) -> RequestRecord:
    """Collapse a concurrent in-library create race, returning the surviving record.

    The active-dedup partial UNIQUE index excludes terminal ``available``, so two
    ``POST /requests`` that both saw the title as already-in-Plex (neither committed
    yet) can each insert a fresh ``available`` row with no IntegrityError backstop.
    Once ours is committed, re-read the OLDEST ``available`` row for this media; if an
    earlier one exists, THIS row is the race loser -> delete it and return the winner,
    so the list/modal shows ONE row. Movie and TV share this (TV reaches ``available``
    via the season rollup, the movie via the in-library short-circuit).

    Ownership (issue #58) — the same invariant as :func:`_subscribe_dedup_winner`
    (never hand back a row the caller's per-user list/get hide): when the earlier
    winner belongs to a DIFFERENT user and the caller is a non-admin, the caller is
    subscribed to that winner before its duplicate row is removed. The display
    remains singular and visibility survives without reassigning ownership.

    An OWNERLESS winner (e.g. an ``X-Api-Key`` automation create) is likewise
    ADOPTED for this user BEFORE the collapse, so the surviving row lands in their
    own list rather than vanishing behind the per-user filter. On a LOST adoption
    race the winner is now foreign: the caller is subscribed before the duplicate
    is deleted, so this path also converges without returning a hidden row.
    """
    winner = await repo.find_earliest_available(tmdb_id, media_type)
    if winner is not None and winner.id != record.id:
        if _owned_by_another_user(winner, user_id, actor_is_admin):
            if user_id is not None:
                await repo.add_subscriber(winner.id, user_id)
            await repo.copy_subscribers(record.id, winner.id)
            await repo.delete(record.id)
            await session.commit()
            return await repo.get(winner.id) or winner
        if (
            winner.user_id is None
            and user_id is not None
            and not await repo.claim_if_unowned(winner.id, user_id)
        ):
            # Lost the adoption race: re-read past this session's stale copy and,
            # if the winner is now another user's row, keep our OWN committed row.
            refreshed = await repo.get_fresh(winner.id) or winner
            if _owned_by_another_user(refreshed, user_id, actor_is_admin):
                await repo.add_subscriber(refreshed.id, user_id)
                await repo.copy_subscribers(record.id, refreshed.id)
                await repo.delete(record.id)
                await session.commit()
                return await repo.get(refreshed.id) or refreshed
        if user_id is not None:
            await repo.add_subscriber(winner.id, user_id)
        await repo.copy_subscribers(record.id, winner.id)
        await repo.delete(record.id)
        await session.commit()
        return await repo.get(winner.id) or winner
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
    actor_is_admin: bool = False,
    library: LibraryPort | None = None,
    seasons: list[int] | None = None,
    episodes: dict[int, list[int]] | None = None,
    force: bool = False,
    expand_shared_tv: bool = False,
) -> RequestRecord:
    """Create a request and return only the request read model."""
    result = await create_request_result(
        session,
        tmdb,
        tmdb_id=tmdb_id,
        media_type=media_type,
        user_id=user_id,
        actor_is_admin=actor_is_admin,
        library=library,
        seasons=seasons,
        episodes=episodes,
        force=force,
        expand_shared_tv=expand_shared_tv,
    )
    return result.record


async def create_request_result(
    session: AsyncSession,
    tmdb: MetadataPort,
    *,
    tmdb_id: int,
    media_type: str,
    user_id: int | None = None,
    actor_is_admin: bool = False,
    library: LibraryPort | None = None,
    seasons: list[int] | None = None,
    episodes: dict[int, list[int]] | None = None,
    force: bool = False,
    expand_shared_tv: bool = False,
) -> CreateRequestResult:
    """Create (or return the existing active) media request for this media.

    Dedups on the ``(tmdb_id, media_type)`` composite via
    :meth:`RequestRepository.find_active`: a non-terminal request for the same
    media is returned unchanged. Otherwise the TMDB detail (incl. art) is resolved
    and a new ``pending`` request is persisted.

    When ``library`` is supplied and the movie is **already in Plex**, the request
    is recorded directly as ``available`` (and ``library_verified_at`` stamped),
    short-circuiting the search/grab — a visible "already in your library" record,
    not a wasted grab. An unconfigured/unreachable Plex skips the check (see
    :func:`_already_in_library`). When a PRIOR terminal row for this media left a
    ``library_path`` breadcrumb (this app placed a file here before), the GUID
    match is additionally corroborated against that path via
    :meth:`LibraryPort.confirm_paths` before minting — a transient/mis-tagged Plex
    GUID that does not survive that corroboration falls through to a normal
    ``pending`` request instead of instant-completing. A media with no prior
    breadcrumb (the common already-owned-by-someone-else case) is unaffected.

    Re-acquire (issue #131): when ``force`` is True the movie already-in-library
    short-circuit's Plex probe and its terminal ``available`` mint are SKIPPED, so a
    title Plex still reports present (its file was deleted/replaced out-of-band)
    yields a fresh ``pending`` request that searches/grabs, rather than a terminal
    ``available`` row with no grab. Every OTHER guard is preserved: the
    ``find_active`` active-request dedup (and its ownership / ownerless-subscription
    decisions) runs unchanged before ``force`` is even consulted, so a foreign
    active request is shared through subscriber membership and a duplicate row is
    impossible (``uq_media_requests_active`` still applies to the inserted
    ``pending`` row). The forced path ALSO takes the same per-media lock and does
    the same under-lock ``find_active`` re-read as the normal short-circuit before
    inserting its ``pending`` row -- otherwise a concurrent normal create could take
    the lock, miss the forced row (still uncommitted, and a ``pending`` that
    ``find_in_library`` never matches), and mint a terminal ``available`` row
    outside ``uq_media_requests_active``, handing that caller a false in-library
    answer while a fresh re-acquire is active. ``force`` is **movie-only**; it is
    ignored for a ``tv`` request -- per-season re-acquisition of a presence-only
    season is the report-issue verb's job (see ``correction_service.report_issue``).

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

    When a non-admin authenticated caller dedups onto another user's row, the
    caller becomes a subscriber and receives that shared row without ownership
    reassignment. Ordinary manual TV dedup does not grow a foreign owner's season
    intent; watchlist sync may explicitly request whole-show expansion through
    ``expand_shared_tv`` because a Plex watchlist represents that complete intent.
    """
    if media_type not in {"movie", "tv"}:
        raise MediaTypeDeferredError(media_type)

    repo = SqlRequestRepository(session)
    existing = await repo.find_active(tmdb_id, media_type)
    if existing is not None:
        # Issue #58: a non-admin authenticated user must not dedup onto an active
        # request OWNED BY A DIFFERENT USER. Falling through would (a) for tv mutate
        # the other user's request by adding this caller's seasons via
        # ensure_seasons below, and (b) return a record the caller's own per-user
        # list/get immediately hid before subscriber membership was introduced.
        # The SAME decision guards the terminal find_in_library short-circuits and
        # the IntegrityError race recovery below — every path that can hand back a
        # dedup winner (see _owned_by_another_user).
        foreign = _owned_by_another_user(existing, user_id, actor_is_admin)
        if foreign:
            existing = await _subscribe_user(session, repo, existing, user_id)
            if media_type != "tv" or not expand_shared_tv:
                return CreateRequestResult(record=existing, created=False)
        # The deduped active request may have no owner (e.g. it was created via the
        # API-key automation path, which carries no user identity). Adopt it for the
        # requesting user so the dedup result appears in THEIR request list instead
        # of succeeding yet vanishing behind the per-user list filter. Never
        # reassigns a request that already belongs to another user.
        if not foreign:
            existing = await _subscribe_dedup_winner(session, repo, existing, user_id)
        existing = await _subscribe_user(session, repo, existing, user_id)
        if media_type == "tv":
            season_plan = await _resolve_tv_season_plan(
                tmdb,
                tmdb_id,
                seasons=seasons,
                episodes=episodes,
            )
            await _ensure_tv_season_plan(
                session,
                library,
                media_request_id=existing.id,
                tmdb_id=tmdb_id,
                plan=season_plan,
            )
            await _merge_tv_request_intent(repo, existing, seasons, episodes)
            await session.commit()
            # ensure_seasons recomputed + persisted the parent rollup; re-read so the
            # returned record's top-level status matches the seasons the response will
            # embed. ``existing`` was captured by find_active BEFORE that rollup write.
            existing = await repo.get(existing.id) or existing
        return CreateRequestResult(record=existing, created=False)

    detail = await _resolve_detail(tmdb, tmdb_id, media_type)

    # Resolve the season list BEFORE anything is persisted: a whole-series
    # request (no explicit ``seasons``) that resolves to NOTHING trackable must
    # never become a 'pending' request with zero SeasonRequest rows (see
    # NoAiredSeasonsError). An explicit (even out-of-range) season list is left
    # alone -- tracking a season Plex/TMDB doesn't have yet is harmless.
    season_numbers: list[int] = []
    season_plan = _TvSeasonPlan(season_numbers=[], waiting_seasons=set())
    if media_type == "tv":
        season_plan = await _resolve_tv_season_plan(
            tmdb,
            tmdb_id,
            seasons=seasons,
            episodes=episodes,
            detail=detail,
            default_waiting_first_season=True,
        )
        season_numbers = season_plan.season_numbers

    initial_status = RequestStatus.pending.value
    # Provenance marker (issue #156; hardened by the Codex round-2 finding below):
    # ``True`` whenever THIS movie's own eviction guard (``repo.
    # latest_request_evicted``) is the reason a fresh 'pending' row is about to be
    # created -- i.e. the newest tracked history for this ``(tmdb_id, media_type)``
    # is 'evicted' -- never for an ordinary request for a movie that was simply
    # never in the library, and never a ``force`` (#148) re-acquire (which skips
    # this guard entirely, see the ``if not force`` branch below). Threaded into
    # ``repo.create`` so the eviction restore's redundant-regrab dedup
    # (``eviction_service._cancel_redundant_movie_regrabs``) can tell its OWN
    # re-grab apart from a deliberate operator re-acquire.
    #
    # Checked UNCONDITIONALLY below for every non-force movie request -- NOT only
    # when this call's own Plex probe proves presence. During the eviction
    # claim/delete window Plex can just as easily ERROR (``_already_in_library``'s
    # best-effort 'no') or correctly report the file already gone as it can still
    # report it present; in every one of those shapes the fresh row this call
    # creates is STILL an in-window eviction regrab, and under-stamping it would
    # leave a genuine duplicate invisible to the restore's dedup (the P2 this
    # closes: a failed purge would then leave BOTH the restored file and a
    # redundant active re-download standing).
    movie_eviction_regrab = False
    if library is not None and media_type == "movie":
        # ``force`` (issue #131, re-acquire): still SKIPS the Plex round-trip in
        # ``_already_in_library`` (the ``force or`` short-circuit -- an operator who
        # already knows the file is gone shouldn't pay for, or risk racing, a
        # presence check whose answer they are about to override).
        plex_present = force or await _already_in_library(library, tmdb_id)
        # Whether a prior terminal row's ``library_path`` breadcrumb existed but Plex
        # could NOT corroborate it -- resolved BEFORE the per-media lock below (see
        # its own comment) and consumed by the mint-'available' branch further down.
        breadcrumb_uncorroborated = False
        if plex_present:
            # Corroborate the in-library short-circuit's breadcrumb BEFORE taking the
            # per-media lock. ``confirm_paths`` is network I/O, and -- exactly like the
            # TV path's Plex crawl below, which is also ordered before its
            # ``acquire_media_lock`` -- it must NEVER run inside the lock's open write
            # transaction: on SQLite (single-writer) the lock-row insert has already
            # opened a write transaction, so a slow/unreachable Plex call held under it
            # would stall every unrelated writer for the full Plex timeout. Only the
            # non-``force`` mint-'available' branch consults this (``force`` never mints
            # 'available'), so the round-trip is skipped for a forced re-acquire. The
            # decision is re-applied under the lock below only after re-reading that no
            # active/terminal row now supersedes it; a benign breadcrumb change in the
            # gap merely falls through to the conservative 'pending'.
            if not force:
                breadcrumb = await repo.latest_library_path(tmdb_id, media_type)
                if breadcrumb is not None:
                    confirmed: frozenset[str]
                    try:
                        confirmed = await library.confirm_paths("movie", [breadcrumb])
                    except (PlexLibraryError, PlexAuthError, NotImplementedError):
                        confirmed = frozenset()
                    breadcrumb_uncorroborated = breadcrumb not in confirmed
            # Dedup the available short-circuit: if this movie is already recorded as
            # in-library, return that row rather than accumulating duplicate 'available'
            # rows (the active-dedup partial index excludes terminal statuses, so it
            # would not catch this). Acquire a per-media DB lock first so PostgreSQL MVCC
            # cannot let two concurrent transactions both miss each other's uncommitted
            # terminal row. A movie REMOVED from Plex reads not-available above and falls
            # through to a normal pending request, so re-requests still work.
            #
            # ``force`` NEVER mints an 'available' row (the eviction guard + terminal
            # in-library dedup below are gated ``not force``). But it MUST participate in
            # the SAME per-media lock and the SAME under-lock active re-read as the normal
            # short-circuit: without them a forced re-acquire inserts its 'pending' row
            # OUTSIDE this lock, so a concurrent normal create can take the lock, MISS the
            # still-uncommitted forced row (its 'pending' is not yet visible, and
            # available/completed-only find_in_library never matches a 'pending'), and
            # mint a terminal 'available' row -- which, being outside
            # ``uq_media_requests_active``, does not collide with the forced 'pending' --
            # handing the caller a false in-library answer while a fresh re-acquire is
            # active. Taking the lock here serializes the two: the normal create either
            # sees the committed forced 'pending' under the lock and dedups onto it, or
            # wins the lock first (a legitimate ordering -- the forced re-acquire simply
            # lands after and creates its own 'pending').
            await repo.acquire_media_lock(tmdb_id, media_type)
            # Re-read the ACTIVE row UNDER the lock: our find_active at the top ran before
            # the lock, so a concurrent re-request for the same movie may have committed an
            # active 'pending' re-grab in the meantime (the evicted-guard branch below, or
            # a concurrent forced re-acquire, mints exactly that -- which find_in_library,
            # available/completed only, would NOT catch). Dedup onto it so two racing
            # re-requests never leave a second row, and in particular never let one mint
            # 'available' over a file another is already re-acquiring just because the
            # newest row is now that concurrent 'pending' one.
            existing_active = await repo.find_active(tmdb_id, media_type)
            if existing_active is not None:
                foreign = _owned_by_another_user(existing_active, user_id, actor_is_admin)
                if foreign:
                    await session.rollback()
                    existing_active = await _subscribe_user(session, repo, existing_active, user_id)
                    if not expand_shared_tv:
                        return CreateRequestResult(record=existing_active, created=False)
                else:
                    existing_active = await _subscribe_dedup_winner(
                        session, repo, existing_active, user_id
                    )
                return CreateRequestResult(record=existing_active, created=False)
        if not force:
            # ``force`` never consults the eviction guard nor dedups onto a terminal
            # in-library row (the operator is deliberately re-acquiring a title Plex
            # still shows present), and its ``initial_status`` stays 'pending'. Only the
            # NON-force path reads the eviction guard and can mint 'available'.
            if await repo.latest_request_evicted(tmdb_id, media_type):
                # The disk-pressure sweep (ADR-0012) most recently reclaimed this
                # movie's file: the row is 'evicted' and either mid-delete or awaiting
                # the post-delete Plex refresh, so trusting THIS call's own Plex
                # reading (present, absent, or erroring -- see above) would be wrong
                # either way: 'present' is STALE and would mint an 'available' row over
                # a file the sweep is about to (or just did) delete (the P1 this
                # closes); 'absent'/erroring still means a fresh re-grab is exactly
                # right, but the row must still carry the marker so a later failed
                # purge's dedup recognizes it as ITS OWN re-grab. Checked BEFORE the
                # find_in_library return below, not after: a media can carry an OLDER
                # stale 'available' row alongside the just-evicted one (the
                # removed-then-reacquired leftover keeps BOTH available rows; the
                # sweep claims only the one it evicts), and returning that leftover
                # here would hand back an in-library answer for content the sweep is
                # deleting -- bypassing this guard entirely. The guard must gate EVERY
                # path that can answer 'available' off Plex presence. Fall through to
                # a normal 'pending' re-grab instead: honesty over silence, and
                # symmetric with the eviction crash-recovery self-heal
                # (``eviction_service``: "a re-request re-grabs it fresh").
                _logger.info(
                    "movie's most-recent request is 'evicted'; re-grabbing as this "
                    "eviction's own in-window regrab rather than trusting this call's "
                    "own Plex reading during the eviction delete window",
                    extra={"tmdb_id": safe_int(tmdb_id)},
                )
                movie_eviction_regrab = True
            elif plex_present:
                in_library = await repo.find_in_library(
                    tmdb_id,
                    media_type,
                    prefer_user_id=_dedup_preference_user_id(user_id, actor_is_admin),
                )
                if in_library is not None:
                    # Issue #58: same ownership decision as the find_active dedup above.
                    # A terminal row owned by another user is shared by subscription.
                    # The owner-preference lookup already picked the caller's OWN row (or an
                    # ownerless subscribable one) over a foreign row when several terminal rows
                    # exist, so sharing a foreign row is the final fallback.
                    if _owned_by_another_user(in_library, user_id, actor_is_admin):
                        await session.rollback()
                        in_library = await _subscribe_user(session, repo, in_library, user_id)
                        return CreateRequestResult(record=in_library, created=False)
                    # And an OWNERLESS in-library row (e.g. an X-Api-Key automation create)
                    # is subscribed for this requester, exactly like the active-dedup path.
                    in_library = await _subscribe_dedup_winner(session, repo, in_library, user_id)
                    return CreateRequestResult(record=in_library, created=False)
                initial_status = RequestStatus.available.value
                # Breadcrumb-gated corroboration (defense-in-depth for a
                # RE-ACQUIRE): reached only when no available/completed
                # ``find_in_library`` target exists yet Plex's GUID says present --
                # exactly the shape a transient/mis-tagged Plex GUID can also
                # produce for a movie THIS app placed before (a prior terminal row
                # left a real ``library_path``). A first-time already-owned movie
                # (never placed by this app) has no breadcrumb and keeps today's
                # behavior unchanged -- only a media with prior history gets the
                # extra check, so the common case never pays for or risks a wasted
                # re-download. When a breadcrumb existed but ``confirm_paths``
                # could not corroborate it (or the corroboration check itself
                # failed), be conservative: fall through to a normal 'pending'
                # request instead of instant-completing on a GUID match this app
                # has independent reason to distrust. The ``confirm_paths``
                # network call itself was made BEFORE the per-media lock (see
                # ``breadcrumb_uncorroborated`` above) so the lock's write
                # transaction is never held across Plex I/O.
                if breadcrumb_uncorroborated:
                    _logger.info(
                        "movie GUID-present but a prior library_path breadcrumb "
                        "did not corroborate; not minting instant-available, "
                        "proceeding as a normal pending request",
                        extra={"tmdb_id": safe_int(tmdb_id)},
                    )
                    initial_status = RequestStatus.pending.value

    if (
        media_type == "tv"
        and library is not None
        and season_numbers
        and not season_plan.episode_scoped
    ):
        # TV in-library dedup — the per-season analogue of the movie short-circuit
        # above. When EVERY requested season is already in Plex AND an
        # available/completed request for this show already exists, return it
        # (tracking any of these seasons it doesn't already list) instead of
        # inserting a duplicate terminal 'available' MediaRequest + season rows: the
        # active-dedup partial index excludes terminal 'available'/'completed', and
        # the movie collapse below is movie-only, so nothing else would catch the
        # duplicate. A show with a NEW (not-yet-present) season falls through to a
        # normal tracked request so the missing season is still searched/grabbed.
        #
        # The Plex-present set is TRUSTED only after subtracting just-evicted
        # seasons (the TV twin of the movie guard ordering above): during the
        # eviction delete window Plex still lists a season the sweep is deleting,
        # and an OLDER stale available request row can exist for this show -- the
        # unsubtracted superset check would dedup onto it and answer in-library
        # for content that is being removed. A season named here that was just
        # evicted therefore falls through to a normal tracked request instead
        # (mirrors ``ensure_seasons``'s own subtraction, which then also steers
        # the season to 'pending').
        present = await _present_seasons_or_empty(library, tmdb_id)
        media_locked = bool(present)
        if present:
            # Serialize + RE-READ the active row under the per-media lock -- the
            # TV twin of the movie path's under-lock re-read above. A second TV
            # re-request can commit an active 'pending' re-grab between the
            # top-of-function find_active and here; ``evicted_seasons`` (keyed on
            # the newest non-cancelled row per season) then sees that newer
            # PENDING row, subtracts nothing, and the superset check below would
            # dedup onto an OLDER stale 'available' row -- answering in-library
            # for a season the sweep is deleting. The lock is taken only AFTER
            # the Plex crawl above (never hold the write transaction open across
            # network I/O; the movie path orders its Plex check the same way).
            await repo.acquire_media_lock(tmdb_id, media_type)
            existing_active = await repo.find_active(tmdb_id, media_type)
            if existing_active is not None:
                foreign = _owned_by_another_user(existing_active, user_id, actor_is_admin)
                if foreign:
                    await session.rollback()
                    existing_active = await _subscribe_user(session, repo, existing_active, user_id)
                    if not expand_shared_tv:
                        return CreateRequestResult(record=existing_active, created=False)
                else:
                    existing_active = await _subscribe_dedup_winner(
                        session, repo, existing_active, user_id
                    )
                # Dedup onto the concurrent re-grab, mirroring the top-of-function
                # dedup path (grow its tracked season set, re-read past the
                # rollup). Release the media lock FIRST: ensure_seasons crawls
                # Plex, and the lock's write transaction must not stay open
                # across that network call (the rollback discards only the lock
                # acquisition -- nothing else is pending in this session here).
                await session.rollback()
                await _ensure_tv_season_plan(
                    session,
                    library,
                    media_request_id=existing_active.id,
                    tmdb_id=tmdb_id,
                    plan=season_plan,
                )
                await _merge_tv_request_intent(repo, existing_active, seasons, episodes)
                await session.commit()
                return CreateRequestResult(
                    record=await repo.get(existing_active.id) or existing_active,
                    created=False,
                )
            present = present - await SqlSeasonRequestRepository(session).evicted_seasons(tmdb_id)
        if present.issuperset(season_numbers):
            in_library = await repo.find_in_library(
                tmdb_id,
                media_type,
                prefer_user_id=_dedup_preference_user_id(user_id, actor_is_admin),
            )
            if in_library is not None:
                # Issue #58: same ownership decision as the movie short-circuit.
                # Manual callers subscribe without growing the owner's seasons;
                # watchlist sync may opt into complete-show expansion below.
                foreign = _owned_by_another_user(in_library, user_id, actor_is_admin)
                if foreign:
                    await session.rollback()
                    in_library = await _subscribe_user(session, repo, in_library, user_id)
                    if not expand_shared_tv:
                        return CreateRequestResult(record=in_library, created=False)
                # Adopt an OWNERLESS in-library row for this requester before growing
                # + returning it, mirroring the movie short-circuit and the active
                # dedup path - else the shared user's own per-user filter hides the
                # very row this returns (issue #58's silent-vanish).
                if not foreign:
                    in_library = await _subscribe_dedup_winner(session, repo, in_library, user_id)
                # Release the media lock BEFORE ensure_seasons -- the same
                # discipline as the existing_active branch above: ensure_seasons
                # crawls Plex, and the lock's write transaction (SQLite:
                # single-writer) must never stay open across a network call,
                # stalling every unrelated writer for the duration of a slow
                # Plex response. Only the lock acquisition is pending in this
                # session here, so the rollback discards nothing else;
                # ensure_seasons itself is race-safe without the lock (the
                # unconditional season unique index + IntegrityError re-read).
                await session.rollback()
                await _ensure_tv_season_plan(
                    session,
                    library,
                    media_request_id=in_library.id,
                    tmdb_id=tmdb_id,
                    plan=season_plan,
                )
                await _merge_tv_request_intent(repo, in_library, seasons, episodes)
                await session.commit()
                return CreateRequestResult(
                    record=await repo.get(in_library.id) or in_library,
                    created=False,
                )
        if media_locked:
            # FALL-THROUGH release (the third branch): a mixed present/missing
            # season set (superset failed) or no in-library row to dedup onto
            # proceeds to the fresh-create path below, whose ensure_seasons
            # performs its own Plex crawl -- release the media lock FIRST, the
            # same discipline as the two dedup branches above (never hold the
            # lock's write transaction across network I/O). Nothing read under
            # the lock needs re-reading after the release: ``existing_active``'s
            # ABSENCE only justified proceeding, and a concurrent active row
            # committing after the release makes the create below collide on
            # ``uq_media_requests_active`` -- resolved by the IntegrityError
            # catch (the DB backstop) exactly like any other create race; and
            # the ``evicted_seasons`` subtraction only fed the superset DECISION
            # (a TV create always starts 'pending', and ensure_seasons re-derives
            # presence + the evicted subtraction FRESH on its own crawl).
            await session.rollback()
    created = True
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
            eviction_regrab=movie_eviction_regrab,
            tv_request_mode=(
                _tv_request_intent(seasons, episodes)[0] if media_type == "tv" else None
            ),
            requested_seasons=(
                _tv_request_intent(seasons, episodes)[1] if media_type == "tv" else None
            ),
            requested_episodes=(
                _tv_request_intent(seasons, episodes)[2] if media_type == "tv" else None
            ),
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
            await _ensure_tv_season_plan(
                session,
                library,
                media_request_id=record.id,
                tmdb_id=tmdb_id,
                plan=season_plan,
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
                winner = await _collapse_available_race(
                    session,
                    repo,
                    record,
                    tmdb_id,
                    media_type,
                    user_id=user_id,
                    actor_is_admin=actor_is_admin,
                )
                if winner.id != record.id:
                    # THIS row (the loser) was deleted, cascading ITS SeasonRequests.
                    # The two racers may have named DIFFERENT seasons, so ensure the
                    # winner also tracks the seasons THIS request asked for -- else the
                    # caller gets back a request that doesn't track the season it just
                    # requested. Then re-read past the merged rollup.
                    await _ensure_tv_season_plan(
                        session,
                        library,
                        media_request_id=winner.id,
                        tmdb_id=tmdb_id,
                        plan=season_plan,
                    )
                    await _merge_tv_request_intent(repo, winner, seasons, episodes)
                    await session.commit()
                    winner = await repo.get(winner.id) or winner
                    created = False
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
        # Issue #58: same ownership decision as the find_active dedup. A non-admin
        # who lost the insert race subscribes to the winner without changing its
        # ownership or ordinary manual TV season intent.
        foreign = _owned_by_another_user(winner, user_id, actor_is_admin)
        if foreign:
            winner = await _subscribe_user(session, repo, winner, user_id)
            if media_type != "tv" or not expand_shared_tv:
                return CreateRequestResult(record=winner, created=False)
        # The recovery winner may be OWNERLESS (a shared user's insert lost the
        # active-unique race to X-Api-Key automation). Subscribe without turning
        # automation provenance into destructive browser authority.
        if not foreign:
            winner = await _subscribe_dedup_winner(session, repo, winner, user_id)
        if media_type == "tv":
            season_plan = await _resolve_tv_season_plan(
                tmdb,
                tmdb_id,
                seasons=seasons,
                episodes=episodes,
            )
            await _ensure_tv_season_plan(
                session,
                library,
                media_request_id=winner.id,
                tmdb_id=tmdb_id,
                plan=season_plan,
            )
            await _merge_tv_request_intent(repo, winner, seasons, episodes)
            await session.commit()
            # Re-read past the rollup ensure_seasons just persisted (``winner`` was
            # captured before it), so the returned status matches the response's seasons.
            winner = await repo.get(winner.id) or winner
        return CreateRequestResult(record=winner, created=False)
    if initial_status == RequestStatus.available.value:
        # Collapse the concurrent movie in-library race (F9) via the shared helper.
        # The remove-then-re-acquire flow is unaffected: when a movie was removed from
        # Plex, _already_in_library() reads False and this branch is skipped, so the
        # legitimate SECOND available row from the normal pending -> download ->
        # mark_available path is never reconciled away.
        collapsed = await _collapse_available_race(
            session,
            repo,
            record,
            tmdb_id,
            media_type,
            user_id=user_id,
            actor_is_admin=actor_is_admin,
        )
        return CreateRequestResult(record=collapsed, created=collapsed.id == record.id)
    return CreateRequestResult(record=record, created=created)


async def list_requests(
    session: AsyncSession,
    status: str | None = None,
) -> list[RequestRecord]:
    """List media requests, optionally filtered by ``status``."""
    return await SqlRequestRepository(session).list_by_status(status)


async def list_requests_for_user(
    session: AsyncSession, user_id: int, status: str | None = None
) -> list[RequestRecord]:
    """List requests visible to a creator or later subscriber."""
    return await SqlRequestRepository(session).list_for_user(user_id, status)


async def is_request_visible_to_user(session: AsyncSession, request_id: int, user_id: int) -> bool:
    """Return whether a shared user subscribes to a request."""
    return await SqlRequestRepository(session).is_subscriber(request_id, user_id)


async def list_subscribers(session: AsyncSession, request_id: int) -> list[int]:
    """Return every subscribed user id, earliest-subscribed first (issue #314).

    Drives the ``can_withdraw``/``has_other_participants``/``is_owner`` DTO
    flags (``web/routers/requests.py:_to_response``) for a single-record
    response; the list endpoint uses the batched :func:`count_subscribers` and
    :func:`list_subscribed_request_ids` instead to avoid an N+1.
    """
    return await SqlRequestRepository(session).list_subscribers(request_id)


async def count_subscribers(session: AsyncSession, request_ids: Sequence[int]) -> dict[int, int]:
    """Batch subscriber counts per request id (issue #314); ``{}`` on empty input."""
    return await SqlRequestRepository(session).count_subscribers(request_ids)


async def list_subscribed_request_ids(session: AsyncSession, user_id: int) -> set[int]:
    """Return the set of request ids ``user_id`` subscribes to (issue #314).

    Used to compute ``can_withdraw``/``has_other_participants`` on the requests
    list endpoint without a per-row query: a non-admin's list is already
    subscriber-filtered (every returned row is in this set by construction),
    while an admin's unfiltered view needs the real membership check (an admin
    is not necessarily a subscriber of every row they can see).
    """
    return await SqlRequestRepository(session).list_subscribed_request_ids(user_id)


def fold_requests_for_display(
    records: list[RequestRecord], *, subscriber_scoped: bool = False
) -> list[RequestRecord]:
    """Collapse duplicate rows per media so the requests list shows ONE row.

    The already-in-library short-circuit (``create_request_result``) can leave
    more than one terminal row for the same media (e.g. a false-``available``
    row off a transient wrong Plex GUID, alongside a later genuine re-grab) —
    display-only surgery over already-loaded records, mirroring
    ``SqlRequestRepository.display_statuses_by_tmdb_ids``'s exact preference:
    within a fold-key group, the first NON-settled (active) row wins, else the
    newest (last) row when every row in the group is settled. Underlying DB
    rows are never mutated — this only decides which rows the CALLER shows.

    Fold key is ``(user_id, tmdb_id, media_type)``, not the bare
    ``(tmdb_id, media_type)``: an admin's UNFILTERED view can legitimately hold
    two DIFFERENT users' rows for the same title (each visible to its own
    owner), and collapsing those together would silently hide one user's
    request from the admin — an honesty regression. For a non-admin caller
    (already filtered to their own rows before this is called) the key reduces
    to ``(tmdb_id, media_type)`` exactly, since every row shares their
    ``user_id``.

    ``records`` is assumed id-ascending (``list_by_status`` orders by id), so
    within a group the first non-settled row encountered is also the
    LOWEST-id active one, matching :func:`SqlRequestRepository.find_active`.
    The result is re-sorted by ``id`` ascending for a deterministic order.
    """
    groups: dict[tuple[int | None, int, str], list[RequestRecord]] = {}
    order: list[tuple[int | None, int, str]] = []
    for record in records:
        key = (
            (None, record.tmdb_id, record.media_type)
            if subscriber_scoped
            else (record.user_id, record.tmdb_id, record.media_type)
        )
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(record)
    chosen: list[RequestRecord] = []
    for key in order:
        group = groups[key]
        active = next((r for r in group if r.status not in _SETTLED_STATUS_VALUES), None)
        chosen.append(active if active is not None else group[-1])
    return sorted(chosen, key=lambda r: r.id)


async def get_request(session: AsyncSession, request_id: int) -> RequestRecord | None:
    """Return the request by id, or ``None`` if absent."""
    return await SqlRequestRepository(session).get(request_id)


async def mark_no_acceptable_release(session: AsyncSession, request_id: int) -> bool:
    """Persist ``no_acceptable_release`` on the request when a grab finds nothing.

    Honesty over silence: a live grab that finds no acceptable candidate returns
    409, but without this the owning request would stay ``downloading`` /
    ``searching`` — a dishonest status asserting progress that is not happening.
    ``no_acceptable_release`` is a visible, retryable state (the operator can
    re-search later), not a silent ``failed``.

    A genuine compare-and-swap (issue #72), not read-then-write: this used to read
    the current status, check it was non-TERMINAL, then write unconditionally --
    a TOCTOU gap a concurrent writer (a lower-ranked auto-grab candidate, a manual
    re-grab) could win in, moving the row to ``downloading`` between the read and
    the write, only to be silently regressed back to this dead-end. ``set_status_
    if_in`` closes the gap: its ``WHERE status IN (...)`` is evaluated by the
    DATABASE at write time, not against this function's (possibly stale) view, so
    the row only ever moves to ``no_acceptable_release`` from
    ``_PARKABLE_REQUEST_STATUS_VALUES`` -- see that constant's comment for exactly
    which statuses (every TERMINAL one, plus ``downloading`` / ``import_blocked``)
    a parking transition must never stomp.

    FLUSH-ONLY (mirrors ``season_request_service``'s module-wide convention): never
    commits or rolls back. The caller owns the commit boundary -- on a WON CAS
    (``True``) so it can commit this write atomically alongside anything else it
    means to land together (e.g. ``auto_grab_service._park``'s backoff write, which
    must NOT be persisted for a park that did not happen); on a LOST CAS
    (``False``) the caller should roll back rather than commit (mirrors
    ``eviction_service``'s double-count guard) -- there is nothing of substance to
    lose (the UPDATE affected zero rows), but rolling back cleanly closes out the
    statement's implicit transaction instead of leaving it dangling.

    Returns ``True`` if this call actually parked the request, ``False`` if a
    concurrent writer already moved it out of the parkable set -- the caller MUST
    treat ``False`` as "leave it alone", never retry the write.
    """
    return await SqlRequestRepository(session).set_status_if_in(
        request_id,
        RequestStatus.no_acceptable_release.value,
        _PARKABLE_REQUEST_STATUS_VALUES,
    )


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
    session: AsyncSession,
    request_id: int,
    *,
    keep_forever: bool,
    restrict_to_user_id: int | None = None,
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

    ``restrict_to_user_id`` confines the title-wide sweep to rows owned by
    that user. The endpoint is open to a request's creator (not just admins),
    but a title's rows can belong to DIFFERENT users; a non-admin passes their
    own ``user_id`` so they only ever pin/unpin their own rows and never flip
    another user's eviction protection. ``None`` (the default) leaves the
    sweep unrestricted for admin/operator use.

    Returns ``None`` when the request does not exist (the router surfaces
    404); otherwise commits and returns the freshly updated TARGET record so
    the endpoint can hand back the new state without a second round trip.
    """
    repo = SqlRequestRepository(session)
    current = await repo.get(request_id)
    if current is None:
        return None
    await repo.set_keep_forever_for_title(
        current.tmdb_id,
        current.media_type,
        keep_forever,
        restrict_to_user_id=restrict_to_user_id,
    )
    await session.commit()
    return await repo.get(request_id)
