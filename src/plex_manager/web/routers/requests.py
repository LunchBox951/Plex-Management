"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import RequestStatus
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import DownloadRecord, RequestRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import correction_service, request_service
from plex_manager.services.correction_service import (
    ActiveDuplicateError,
    DownloadClientRequiredError,
    ImportInProgressError,
    MediaRootUnavailableError,
    NotCancellableError,
    NotReportableError,
    ReportSeasonRequiredError,
    SeasonNotFoundError,
    WithdrawalBlockedActiveError,
)
from plex_manager.services.library_roots import LibraryRoots
from plex_manager.services.request_service import MediaNotFoundError, NoAiredSeasonsError
from plex_manager.web.deps import (
    AuthContext,
    ServiceNotConfiguredError,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_downloads_host_root,
    get_eviction_filesystem,
    get_library,
    get_library_optional,
    get_movies_root_optional,
    get_parser,
    get_prowlarr,
    get_qbittorrent,
    get_qbittorrent_optional,
    get_quality_profile,
    get_session,
    get_tmdb,
    get_tv_root_optional,
    require_api_key,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import publish_realtime
from plex_manager.web.schemas import (
    CreateRequestBody,
    ErrorDetail,
    ErrorEnvelope,
    KeepForeverBody,
    ReportIssueBody,
    RequestListResponse,
    RequestResponse,
    SeasonStatus,
    ServiceNotConfiguredErrorDetail,
    WithdrawSubscriptionResponse,
)

if TYPE_CHECKING:
    from plex_manager.ports.repositories import SeasonRequestRecord

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/requests",
    tags=["requests"],
)

# NB: no 409 "media type deferred" here -- ``CreateRequestBody.media_type`` is
# ``Literal["movie", "tv"]``, so any other value is already a 422 at validation;
# the service-level ``MediaTypeDeferredError`` remains only as a defensive guard
# for internal (non-HTTP) callers.
_CREATE_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": RequestResponse, "description": "Existing matching request"},
    404: {"model": ErrorDetail, "description": "Media not found"},
}

# The ONE download state whose ``progress`` is a live transfer percentage. Every
# other non-terminal state stays in ``list_active_for_requests``' batch for
# ownership purposes but is deliberately excluded from the projection:
# ``client_missing`` (the torrent vanished from qBittorrent, so its stored
# progress is a frozen last-known value while the request rides out the grace
# window), ``metadata_fetching`` (no payload transfer exists yet to have a
# truthful percentage), and ``import_pending`` (the transfer is over -- the wait
# is for import, which a percent bar would misreport as still-downloading).
# Mirrors the Queue UI, which shows transfer progress ONLY for
# ``status === 'downloading'`` and an honest state label for everything else.
_LIVE_PROGRESS_DOWNLOAD_STATUSES: frozenset[str] = frozenset({DownloadState.Downloading.value})

_REPORT_ISSUE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Request or season not found"},
    # This status code has THREE distinct producers, so ALL shapes are documented
    # via a union "model" (the same pattern as the cancel endpoint's 409 below --
    # FastAPI expands ``X | Y | Z`` into an anyOf AND registers every member's
    # component schema itself, so each ref is self-registering rather than
    # depending on some OTHER endpoint happening to reference the same model
    # elsewhere): the string-detail ``HTTPException`` 409s (``not_reportable`` /
    # ``active_duplicate`` -- ``ErrorDetail``), the ``AppError`` 409
    # (``media_root_unavailable`` -- an ``ErrorEnvelope`` whose message/hint/
    # diagnostics carry the actionable broken-root guidance), and
    # ``ServiceNotConfiguredError``'s 409 ``service_not_configured`` (issue #291 --
    # this endpoint requires Plex/qBittorrent/Prowlarr via NON-optional deps, so an
    # install missing any of them 409s the same way cancel's does). Declaring
    # fewer models would make the generated TS client mis-model the missing shape(s).
    409: {
        "model": ErrorDetail | ErrorEnvelope | ServiceNotConfiguredErrorDetail,
        "description": (
            "Not reportable in its current state, an active duplicate exists, the "
            "title's library folder isn't reachable from the app, or a required "
            "service (Plex/qBittorrent/Prowlarr) is not configured"
        ),
    },
    # A TV request reported with no ``season`` in the body (``ReportSeasonRequiredError``
    # -- ``report_requires_season``), raised BEFORE any state change. Documented
    # alongside FastAPI's own body-validation 422 via anyOf, mirroring the grab
    # endpoint's ``_GRAB_ERROR_RESPONSES`` pattern -- this status code has two
    # distinct producers here too.
    422: {
        "description": "Validation error, or a tv request reported without a season",
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/ErrorDetail"},
                    ]
                }
            }
        },
    },
}

# The cancel endpoint's manually-raised statuses (ADR-0014 correction verb, extended
# by issue #314): 404 ``request_not_found``, 409 ``not_cancellable``/
# ``import_in_progress``/``has_other_participants`` (plain ``HTTPException`` --
# ``ErrorDetail`` wire shape), and ``ServiceNotConfiguredError``'s 409
# ``service_not_configured`` (rendered by the app-wide handler, NOT a plain
# ``HTTPException``). That handler's body ALWAYS carries a ``service`` field
# (``{"detail": "service_not_configured", "service": "qbittorrent"}``) that
# ``ErrorDetail`` has no field for, so this status is documented across BOTH
# shapes via a union "model" (FastAPI expands ``X | Y`` into an anyOf AND
# registers both members' component schemas -- a raw ``content``/``schema``
# dict with a hand-written ``$ref`` does NOT register a schema that isn't
# ALSO used as a bare "model" somewhere else, which would leave
# ``ServiceNotConfiguredErrorDetail`` a dangling ref). This documents
# ``service`` on the generated client type, so callers can route the operator
# straight to qBittorrent setup instead of losing the field to the generic shape.
#
# ``has_other_participants`` (issue #314): a non-admin owner's ``POST /cancel``
# with other subscribers still attached is refused rather than silently
# hard-cancelling their shared request out from under them -- the UI routes
# that case to ``DELETE /subscription`` (Withdraw) instead.
_CANCEL_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Request not found"},
    409: {
        "model": ErrorDetail | ServiceNotConfiguredErrorDetail,
        "description": (
            "Not cancellable in its current state, an import is in progress, a "
            "non-admin owner has other participants and must withdraw instead "
            "(``has_other_participants``), or qBittorrent is required but not "
            "configured"
        ),
    },
}

# The withdraw endpoint's manually-raised statuses (issue #314): 404
# ``request_not_found`` (unknown id, or the caller is not a subscriber -- the same
# "hide non-participant existence" posture as the cancel/report-issue mutator
# guard), 409 ``import_in_progress``/``not_cancellable`` (the last-participant
# settle branch reuses ``cancel_request``'s own refusals verbatim),
# ``withdrawal_blocked_active_request`` (the last participant tried to withdraw
# from an ACTIVE non-cancellable row -- ``import_blocked``/``partially_available``/
# ``completed`` -- which is still dedup-blocking yet neither tearable-down nor
# genuinely settled; ``completed`` is imported-but-awaiting-Plex-confirmation), and
# ``ServiceNotConfiguredError``'s 409 ``service_not_configured`` (same shape/
# rationale as the cancel endpoint's, above).
_WITHDRAW_SUBSCRIPTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorDetail,
        "description": "Request not found, or the caller does not subscribe to it",
    },
    409: {
        "model": ErrorDetail | ServiceNotConfiguredErrorDetail,
        "description": (
            "An import is in progress (``import_in_progress``), the "
            "last-participant settle hit a not-cancellable TV season "
            "(``not_cancellable``), the last participant tried to withdraw from "
            "an active non-cancellable request (``withdrawal_blocked_active_"
            "request``), or qBittorrent is required but not configured "
            "(``service_not_configured``)"
        ),
    },
}


def _can_mutate_request(auth: AuthContext, record: RequestRecord) -> bool:
    """Return whether this caller owns the request or has operator access."""
    return auth.is_admin or (auth.user_id is not None and record.user_id == auth.user_id)


def _is_owner(auth: AuthContext, record: RequestRecord) -> bool:
    """Return whether this caller is the request's CURRENT owner (issue #314).

    ``user_id`` can hand off on withdrawal (``correction_service.
    withdraw_participant``), so this is deliberately re-derived per response
    rather than cached from creation time.
    """
    return auth.user_id is not None and record.user_id == auth.user_id


async def _require_request_mutator(
    request_id: int,
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthContext:
    """Allow only the creator or an admin, hiding non-owned request existence."""
    record = await request_service.get_request(session, request_id)
    if record is None or not _can_mutate_request(auth, record):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return auth


async def _require_subscriber(
    request_id: int,
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthContext:
    """Allow only a subscriber (participant) of the request (issue #314).

    Withdrawal is a participant's own capability, not an admin one: an admin
    who is NOT a subscriber of this row gets 404 here (the same "hide
    non-owned/non-participant existence" posture as ``_require_request_mutator``)
    and uses ``POST /cancel`` instead. The legacy app API key carries no user
    identity (``auth.user_id is None``) and so can never withdraw.
    """
    record = await request_service.get_request(session, request_id)
    if (
        record is None
        or auth.user_id is None
        or not await request_service.is_request_visible_to_user(session, request_id, auth.user_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return auth


async def _subscriber_flags(
    session: AsyncSession, record: RequestRecord, auth: AuthContext
) -> tuple[bool, bool]:
    """Return ``(can_withdraw, has_other_participants)`` for the caller (issue #314).

    A single-record analogue of the list endpoint's batched
    ``count_subscribers``/``list_subscribed_request_ids`` -- used everywhere a
    ``RequestResponse`` is built for exactly one row (create/get/keep-forever/
    report-issue/cancel), where batching would buy nothing.
    """
    if auth.user_id is None:
        return False, False
    subscribers = await request_service.list_subscribers(session, record.id)
    can_withdraw = auth.user_id in subscribers
    has_other_participants = any(uid != auth.user_id for uid in subscribers)
    return can_withdraw, has_other_participants


async def _to_response(
    session: AsyncSession,
    record: RequestRecord,
    seasons_by_request: dict[int, list[SeasonRequestRecord]] | None = None,
    episode_counts_by_season_id: dict[int, tuple[int, int]] | None = None,
    active_downloads_by_request: dict[int, list[DownloadRecord]] | None = None,
    *,
    can_mutate: bool = False,
    is_owner: bool = False,
    can_withdraw: bool = False,
    has_other_participants: bool = False,
) -> RequestResponse:
    """Map a request record to the wire DTO, embedding its per-season rollup for tv.

    ``seasons_by_request`` is an optional pre-fetched ``{media_request_id:
    [SeasonRequestRecord, ...]}`` map -- see ``list_requests_endpoint``, which
    fetches EVERY tracked show's season rows in ONE batched query up front (via
    ``SeasonRequestRepository.list_for_requests``) rather than calling this
    per-row, which would otherwise issue one ``list_for_request`` query per tv
    request on the list endpoint (an N+1). When omitted (the single-record ``GET
    /requests/{id}`` path, where batching buys nothing), a tv record fetches its
    OWN season rows directly. A movie record's ``seasons`` is always ``None`` --
    movies have no ``SeasonRequest`` rows.

    ``episode_counts_by_season_id`` (ADR-0020, issue #178) is likewise an optional
    pre-fetched ``{season_request_id: (imported_count, target_count)}`` map for the
    "N/M episodes" badge; when omitted, this function does its OWN small batched
    read (one query for however many seasons this single record has). A season
    absent from the map (no tracked ``season_episode_states`` rows -- the common
    clean-single-pack-import case) renders both counts ``None``, never a fabricated
    ``0/0``.

    ``active_downloads_by_request`` follows the same batching rule for byte
    progress. The list endpoint supplies one actor-filtered batch; single-record
    and mutation responses perform one corresponding request-id read. Progress is
    truthful only for a displayed ``downloading`` request with exactly one
    physical download actually transferring (``_LIVE_PROGRESS_DOWNLOAD_STATUSES``
    -- client-missing/metadata/import-pending rows never drive the bar).
    Multiple concurrent live TV-season downloads are intentionally ambiguous --
    never averaged, summed, or selected arbitrarily.
    """
    seasons: list[SeasonRequestRecord] | None = None
    if record.media_type == "tv":
        if seasons_by_request is not None:
            seasons = seasons_by_request.get(record.id, [])
        else:
            seasons = await SqlSeasonRequestRepository(session).list_for_request(record.id)
    episode_counts = episode_counts_by_season_id
    if episode_counts is None and seasons:
        episode_counts = await SqlSeasonEpisodeStateRepository(session).counts_for_seasons(
            [s.id for s in seasons]
        )
    active_downloads = active_downloads_by_request
    if active_downloads is None:
        active_downloads = await SqlDownloadRepository(session).list_active_for_requests(
            [record.id]
        )
    # Only ``downloading`` rows may drive the bar: every other non-terminal row
    # still belongs to the request (and stays in the batch) but has no truthful
    # transfer percentage to show (see ``_LIVE_PROGRESS_DOWNLOAD_STATUSES``).
    # The exact-one rule is applied AFTER this filter: one live transfer next to
    # a client-missing/metadata/import-pending sibling is still one honest
    # number, while two live transfers remain ambiguous.
    live_downloads = [
        download
        for download in active_downloads.get(record.id, [])
        if download.status in _LIVE_PROGRESS_DOWNLOAD_STATUSES
    ]
    download_progress = (
        live_downloads[0].progress
        if record.status == "downloading" and len(live_downloads) == 1
        else None
    )
    return RequestResponse(
        id=record.id,
        tmdb_id=record.tmdb_id,
        media_type=record.media_type,
        title=record.title,
        status=cast(RequestStatus, record.status),
        year=record.year,
        is_anime=record.is_anime,
        poster_url=record.poster_url,
        backdrop_url=record.backdrop_url,
        tv_request_mode=record.tv_request_mode,
        requested_seasons=list(record.requested_seasons) if record.requested_seasons else None,
        requested_episodes=(
            {season: list(values) for season, values in record.requested_episodes.items()}
            if record.requested_episodes
            else None
        ),
        seasons=(
            [
                SeasonStatus(
                    season_number=s.season_number,
                    status=cast(RequestStatus, s.status),
                    installed_quality_id=s.installed_quality_id,
                    installed_profile_index=s.installed_profile_index,
                    imported_episode_count=((episode_counts or {}).get(s.id, (None, None))[0]),
                    target_episode_count=((episode_counts or {}).get(s.id, (None, None))[1]),
                )
                for s in seasons
            ]
            if seasons is not None
            else None
        ),
        download_progress=download_progress,
        keep_forever=record.keep_forever,
        can_mutate=can_mutate,
        is_owner=is_owner,
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=_CREATE_REQUEST_RESPONSES,
)
async def create_request_endpoint(
    body: CreateRequestBody,
    response: Response,
    http_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
) -> RequestResponse:
    """Create a request (or return the existing active one for this media).

    If Plex is configured and the movie is already in the library, the request is
    recorded directly as ``available`` (no needless search/grab). For a tv
    request, ``body.seasons`` (omitted/empty = the whole aired series) is threaded
    to ``request_service.create_request``, which tracks each named season as its
    own ``SeasonRequest`` row -- including on the dedup path, where a repeat POST
    with a NEW season list grows the tracked set rather than being dropped.

    Re-acquire (issue #131): ``body.force`` (movie-only) bypasses the
    already-in-library short-circuit so a title Plex still reports present -- but
    whose file was deleted/replaced out-of-band -- yields a fresh, grabbable
    ``pending`` request instead of a terminal ``available`` one. Same authZ as any
    create; same response map (no new status codes) -- every dedup/ownership
    guard below still applies unchanged.
    """
    try:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
            library=library,
            seasons=body.seasons,
            episodes=body.episodes,
            force=bool(body.force),
            user_id=auth.user_id,
            actor_is_admin=auth.is_admin,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media_not_found",
        ) from exc
    except NoAiredSeasonsError as exc:
        # The show exists in TMDB but resolved to zero trackable seasons (a data
        # gap, or a specials-only show) -- an honest 404, never a persisted
        # 'pending' request with nothing to search/grab.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_aired_seasons",
        ) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    can_withdraw, has_other_participants = await _subscriber_flags(session, result.record, auth)
    response_body = await _to_response(
        session,
        result.record,
        can_mutate=_can_mutate_request(auth, result.record),
        is_owner=_is_owner(auth, result.record),
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )
    publish_realtime(
        http_request.app,
        ("requests", "discover"),
        reason="request_created" if result.created else "request_updated",
    )
    return response_body


@router.get(
    "",
    responses={
        422: {
            "model": ErrorDetail,
            "description": (
                "``cursor_requires_limit`` -- ``cursor`` was supplied without ``limit`` "
                "(a cursor only means anything within the paginated mode)"
            ),
        },
    },
)
async def list_requests_endpoint(
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[
        int | None,
        Query(
            ge=1,
            le=200,
            description=(
                "Page size for keyset-paginated RAW request history (issue #218). "
                "Omitted = the legacy whole-list mode (folded, unpaginated) -- "
                "unchanged for existing clients."
            ),
        ),
    ] = None,
    cursor: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "Keyset cursor: the previous page's ``next_cursor``. Only rows with "
                "``id < cursor`` are returned (newest first). Requires ``limit``."
            ),
        ),
    ] = None,
) -> RequestListResponse:
    """List media requests -- whole-list (legacy) or one keyset history page (#218).

    **Paginated mode** (``limit`` supplied): one bounded page of RAW lifetime
    history rows, newest (highest id) first, plus ``next_cursor`` (``null`` when
    exhausted). Shared-user visibility is applied IN SQL before any row is
    materialized, the page is never display-folded (a fold group can span pages;
    the folded live-state view is issue #218's phase-2 compact lookup), and every
    batched enrichment below (seasons, episode counts, downloads, subscriber
    flags) is scoped to exactly the page's ids. ``id`` is unique and monotonic,
    so the keyset is total (no ties) and no OFFSET or COUNT is ever issued.

    **Legacy mode** (no ``limit``): the pre-#218 behavior byte-for-byte -- the
    whole visible set, display-folded -- plus an ignorable ``next_cursor: null``,
    so a cached SPA bundle from before this change keeps working against the new
    API during a rollout. ``cursor`` without ``limit`` is refused (422
    ``cursor_requires_limit``) rather than silently ignored.
    """
    next_cursor: int | None = None
    if limit is not None:
        records, next_cursor = await request_service.list_requests_page(
            session,
            # Same visibility scope as the legacy branch below: admins and the
            # (admin-equivalent) API key see every row; a shared user's page is
            # subscriber-filtered in SQL.
            for_user_id=(None if auth.is_admin or auth.user_id is None else auth.user_id),
            before_id=cursor,
            limit=limit,
        )
    elif cursor is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="cursor_requires_limit"
        )
    elif auth.is_admin or auth.user_id is None:
        records = await request_service.list_requests(session)
    else:
        records = await request_service.list_requests_for_user(session, auth.user_id)
    if limit is None:
        # Fold duplicate rows for the same media (e.g. a healed-but-not-yet-collapsed
        # false-``available`` row alongside a genuine re-grab) down to ONE visible
        # row per the requester's own preference order -- AFTER the per-user filter
        # above, so a non-admin's own visible row is never folded onto a row they
        # cannot see. Underlying rows are untouched (see the helper's docstring).
        # LEGACY MODE ONLY: a history page is raw rows by contract (see above).
        records = request_service.fold_requests_for_display(
            records, subscriber_scoped=not auth.is_admin
        )
    # Batch active physical downloads only AFTER actor filtering + display folding.
    # This preserves the shared-user boundary and gives every visible request a
    # consistent progress projection without calling the admin-only queue endpoint
    # or issuing one query per row.
    active_downloads_by_request = await SqlDownloadRepository(session).list_active_for_requests(
        [r.id for r in records]
    )
    # Batch every tv row's season rows in ONE query (avoids an N+1 query per tv
    # request that calling ``_to_response`` per-row without this would cause).
    tv_ids = [r.id for r in records if r.media_type == "tv"]
    seasons_by_request = await SqlSeasonRequestRepository(session).list_for_requests(tv_ids)
    # Likewise batch the episode-fallback "N/M" counts across EVERY season row on
    # the page in ONE query (ADR-0020, issue #178) -- avoids an N+1 that calling
    # ``_to_response`` per-row without this would otherwise cause.
    all_season_ids = [s.id for seasons in seasons_by_request.values() for s in seasons]
    episode_counts_by_season_id = await SqlSeasonEpisodeStateRepository(session).counts_for_seasons(
        all_season_ids
    )
    # Batch the issue #314 subscriber-control flags too: one count-per-request query
    # (``has_other_participants``) plus one membership-set query for the caller
    # (``can_withdraw``) instead of a per-row subscriber lookup. A non-admin's list
    # is already subscriber-filtered above, so every row is in the caller's own
    # membership set by construction; an admin's UNFILTERED view is not, so the real
    # membership check still matters there (see ``list_subscribed_request_ids``).
    subscriber_counts = await request_service.count_subscribers(session, [r.id for r in records])
    subscribed_ids: set[int] = (
        await request_service.list_subscribed_request_ids(session, auth.user_id)
        if auth.user_id is not None
        else set()
    )
    return RequestListResponse(
        requests=[
            await _to_response(
                session,
                r,
                seasons_by_request,
                episode_counts_by_season_id,
                active_downloads_by_request,
                can_mutate=_can_mutate_request(auth, r),
                is_owner=_is_owner(auth, r),
                can_withdraw=r.id in subscribed_ids,
                has_other_participants=(
                    subscriber_counts.get(r.id, 0) - (1 if r.id in subscribed_ids else 0) > 0
                ),
            )
            for r in records
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/{request_id}",
    responses={404: {"model": ErrorDetail, "description": "Request not found"}},
)
async def get_request_endpoint(
    request_id: int,
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Return a single media request, or 404."""
    record = await request_service.get_request(session, request_id)
    visible = (
        record is not None
        and auth.user_id is not None
        and await request_service.is_request_visible_to_user(session, request_id, auth.user_id)
    )
    if record is None or (not auth.is_admin and not visible):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    can_withdraw, has_other_participants = await _subscriber_flags(session, record, auth)
    return await _to_response(
        session,
        record,
        can_mutate=_can_mutate_request(auth, record),
        is_owner=_is_owner(auth, record),
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )


@router.post("/{request_id}/keep-forever")
async def keep_forever_endpoint(
    request_id: int,
    body: KeepForeverBody,
    http_request: Request,
    auth: Annotated[AuthContext, Depends(_require_request_mutator)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Set or clear the operator's "keep forever" pin (ADR-0012).

    The north-star #1 correction path for "don't let the eviction sweep touch
    this one": a pinned title (or, for a show, every one of its seasons -- the
    pin lives on the parent) is never selected by ``domain/eviction.py``
    regardless of watch state or disk pressure. A 404 for an unknown id, never
    a silent no-op. Open to the request's creator or an admin; a non-admin only
    ever moves their own rows, never another user's eviction protection.
    """
    # ``_require_request_mutator`` already confirmed the caller owns this row (or
    # is an admin). Keep-forever is a title-wide sweep and a title's rows can
    # belong to DIFFERENT users, so confine a non-admin's sweep to their own
    # rows -- toggling their row must never silently flip another user's pin.
    # Admins keep the unrestricted, whole-title sweep.
    record = await request_service.set_keep_forever(
        session,
        request_id,
        keep_forever=body.keep_forever,
        restrict_to_user_id=None if auth.is_admin else auth.user_id,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    can_withdraw, has_other_participants = await _subscriber_flags(session, record, auth)
    response_body = await _to_response(
        session,
        record,
        can_mutate=True,
        is_owner=_is_owner(auth, record),
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )
    publish_realtime(
        http_request.app,
        ("requests", "discover", "ops:disk"),
        reason="request_updated",
    )
    return response_body


@router.post("/{request_id}/report-issue", responses=_REPORT_ISSUE_RESPONSES)
async def report_issue_endpoint(
    request_id: int,
    body: ReportIssueBody,
    http_request: Request,
    auth: Annotated[AuthContext, Depends(_require_request_mutator)],
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    library: Annotated[LibraryPort, Depends(get_library)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
    movies_root: Annotated[str | None, Depends(get_movies_root_optional)],
    tv_root: Annotated[str | None, Depends(get_tv_root_optional)],
    anime_movie_root: Annotated[str | None, Depends(get_anime_movie_root_optional)],
    anime_tv_root: Annotated[str | None, Depends(get_anime_tv_root_optional)],
    downloads_host_root: Annotated[str, Depends(get_downloads_host_root)],
) -> RequestResponse:
    """Report a bad imported/available movie or TV season (ADR-0014).

    Blocklists the culprit release, removes its torrent + the library file, and
    synchronously re-searches for a DIFFERENT release (the honest
    ``no_acceptable_release`` park if nothing is acceptable). Requires Plex +
    qBittorrent + Prowlarr configured (their deps 409 ``service_not_configured``
    otherwise). The correction-without-a-terminal button for "this file is bad".
    """
    record = await request_service.get_request(session, request_id)
    if record is None:  # Defensive: the authorization dependency already checked this row.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    # The purge target's root: an unmounted/empty root is refused inside the service
    # (MediaRootUnavailableError -> 409). Build the root-scoped filesystem the same
    # way the eviction trigger does -- the ONLY FileSystemPort whose delete() guard
    # has real roots to check against (see get_eviction_filesystem).
    #
    # ADR-0015 fix: hand the service ALL configured roots (as a typed bundle) and let
    # it derive which one to mount-check FROM the stored library_path breadcrumb --
    # the DEEPEST containing root, so a nested anime root is verified itself, never a
    # mounted parent -- rather than picking a single root here from ``is_anime`` +
    # the currently-configured anime root. That earlier is_anime-based pick was wrong
    # whenever the breadcrumb lived under a DIFFERENT root than the current config
    # implies -- e.g. anime imported BEFORE an anime root was configured, whose file
    # is under movies_root/tv_root: it would 409 against an empty newly-configured
    # anime root, or (worse) wave the check through against a mounted anime root
    # while the real root was unmounted and strand the old file. A row with NO
    # breadcrumb falls back to the media-type-appropriate root inside the service
    # (``LibraryRoots.fallback_for``). The delete-guard's fs is built from the SAME
    # root set, so a breadcrumb under whichever root holds it is deletable.
    roots = LibraryRoots(
        movies=movies_root,
        tv=tv_root,
        anime_movie=anime_movie_root,
        anime_tv=anime_tv_root,
    )
    fs = get_eviction_filesystem(movies_root, tv_root, anime_movie_root, anime_tv_root)
    try:
        updated = await correction_service.report_issue(
            session,
            qbt,
            fs,
            library,
            prowlarr,
            parser,
            profile,
            request_id=request_id,
            reason=body.reason,
            season=body.season,
            roots=roots,
            save_path=downloads_host_root,
        )
    except correction_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        ) from exc
    except ReportSeasonRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="report_requires_season"
        ) from exc
    except SeasonNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="season_not_found"
        ) from exc
    except NotReportableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_reportable") from exc
    except ActiveDuplicateError as exc:
        # A newer active request already owns this media's dedup slot -- re-arming the
        # reported (settled) row would collide. Refused before any blocklist/purge, so
        # nothing was touched; the operator acts on the live active request instead.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="active_duplicate"
        ) from exc
    except MediaRootUnavailableError as exc:
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="media_root_unavailable",
            message="The library folder for this title isn't reachable, so it can't be "
            "re-requested safely.",
            hint="Check the folder is mounted and visible to Plex Manager "
            "(Settings → Library), then try again.",
            diagnostics=({"root": exc.root_path} if auth.is_admin and exc.root_path else None),
        ) from exc
    can_withdraw, has_other_participants = await _subscriber_flags(session, updated, auth)
    response_body = await _to_response(
        session,
        updated,
        can_mutate=True,
        is_owner=_is_owner(auth, updated),
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )
    publish_realtime(
        http_request.app,
        ("requests", "queue", "blocklist", "discover"),
        reason="report_issue",
    )
    return response_body


@router.post("/{request_id}/cancel", responses=_CANCEL_REQUEST_RESPONSES)
async def cancel_request_endpoint(
    request_id: int,
    http_request: Request,
    auth: Annotated[AuthContext, Depends(_require_request_mutator)],
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort | None, Depends(get_qbittorrent_optional)],
) -> RequestResponse:
    """Cancel a not-yet-imported request (ADR-0014): drop active torrent(s), settle.

    Removes any active torrent(s) WITH data (best-effort) and flips the request
    (and every tracked season, for tv) to the settled ``cancelled`` status; the
    row is kept for history and nothing is re-grabbed. A request past the
    not-yet-imported stage is refused (409 ``not_cancellable``) -- use report-issue
    to redo an imported title instead.

    ``POST /cancel`` is a HARD cancel of the whole request -- it never silently
    removes just the caller. An admin may always hard-cancel. A non-admin owner
    may too, but ONLY when they are the request's sole participant; with other
    subscribers still attached, this refuses 409 ``has_other_participants``
    (issue #314) -- the collaborative correction path for that case is
    ``DELETE /subscription`` (Withdraw), which hands ownership off instead of
    cancelling co-participants' shared request out from under them.

    qBittorrent is resolved OPTIONALLY (``get_qbittorrent_optional``): a cancel for a
    ``pending``/``searching``/``no_acceptable_release`` request with NO active download
    rows is a pure DB settle that never touches the client, so it still works on an
    install with qBittorrent unconfigured. When there ARE active torrents to remove but
    the client is unconfigured, the service refuses up front (409
    ``service_not_configured``) rather than silently leaking a seeding torrent.
    """
    try:
        if auth.is_admin:
            updated = await correction_service.cancel_request(session, qbt, request_id=request_id)
        else:
            if auth.user_id is None:  # pragma: no cover - defensive; the mutator dependency
                # already required a user-owned row match, which is impossible without a
                # user identity -- this can never actually be reached.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
                )
            updated = await correction_service.cancel_request_as_owner(
                session, qbt, request_id=request_id, user_id=auth.user_id
            )
    except correction_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        ) from exc
    except correction_service.HasOtherParticipantsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="has_other_participants"
        ) from exc
    except NotCancellableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_cancellable") from exc
    except ImportInProgressError as exc:
        # A download is finalizing its import: cancelling now would race the importer
        # and could strand a placed file under a cancelled request. Honest, retryable
        # 409 -- the operator retries once the import lands (report-issue takes over).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="import_in_progress"
        ) from exc
    except DownloadClientRequiredError as exc:
        # Active torrent(s) to remove, but qBittorrent is unconfigured. Surface the same
        # honest 409 ``service_not_configured`` the mark-failed endpoint uses -- refused
        # before any state change, so nothing was settled or removed.
        raise ServiceNotConfiguredError("qbittorrent") from exc
    can_withdraw, has_other_participants = await _subscriber_flags(session, updated, auth)
    response_body = await _to_response(
        session,
        updated,
        can_mutate=True,
        is_owner=_is_owner(auth, updated),
        can_withdraw=can_withdraw,
        has_other_participants=has_other_participants,
    )
    publish_realtime(
        http_request.app,
        ("requests", "queue", "discover"),
        reason="cancel_request",
    )
    return response_body


@router.delete(
    "/{request_id}/subscription",
    responses=_WITHDRAW_SUBSCRIPTION_RESPONSES,
)
async def withdraw_subscription_endpoint(
    request_id: int,
    http_request: Request,
    auth: Annotated[AuthContext, Depends(_require_subscriber)],
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort | None, Depends(get_qbittorrent_optional)],
) -> WithdrawSubscriptionResponse:
    """Withdraw the caller's OWN subscription from a shared request (issue #314).

    The collaborative counterpart to ``POST /cancel``: removes only the caller's
    participation, never the whole request out from under other subscribers. If
    OTHER subscribers remain, this is a mere subscription removal (with an
    ownership handoff to the earliest remaining subscriber, if the caller was
    the owner) -- nothing else is touched. If the caller is the LAST
    participant, this settles like a normal cancel (teardown + ``cancelled``)
    for a not-yet-imported request, or simply removes the subscription for an
    already-terminal one. See ``correction_service.withdraw_participant``'s
    docstring for the full matrix.

    Returns ``{"settled": bool}`` -- the authoritative under-lock outcome
    (:class:`correction_service.WithdrawOutcome`): ``True`` only when the
    last-participant cancel branch ran and the request settled ``cancelled``
    (any active download, IF one existed, was removed -- a
    pending/searching/no_acceptable_release/waiting_for_air_date row settles
    purely in the DB with no torrent to touch, so ``settled: true`` must never
    be presented as "a download was removed"); ``False`` for a mere
    removal/handoff. The caller keys its success toast off THIS rather than a
    click-time snapshot a concurrent join/withdraw or status advance could have
    made stale (#351); the caller's own row still simply drops out of their
    next ``GET /requests``.
    """
    if auth.user_id is None:  # pragma: no cover - defensive; ``_require_subscriber``
        # already required real subscriber membership, which is impossible without
        # a user identity -- this can never actually be reached.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    try:
        outcome = await correction_service.withdraw_participant(
            session, qbt, request_id=request_id, user_id=auth.user_id
        )
    except correction_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        ) from exc
    except NotCancellableError as exc:
        # Defensive: a mere-removal (others remain) never settles a done row, but the
        # last-participant branch reuses cancel_request's own TV per-season guard,
        # which CAN raise this (a done season under an otherwise-cancellable parent
        # rollup) -- surfaced the same honest, retryable 409 the cancel endpoint uses.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_cancellable") from exc
    except WithdrawalBlockedActiveError as exc:
        # The last participant tried to withdraw from an ACTIVE non-cancellable row
        # (import_blocked / partially_available / completed): still dedup-blocking,
        # yet neither tearable-down nor genuinely settled. Refused with an honest,
        # actionable 409 -- resolve the import, let the in-flight seasons settle, or
        # let the Plex confirmation land (completed -> available) first, then withdraw.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="withdrawal_blocked_active_request"
        ) from exc
    except ImportInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="import_in_progress"
        ) from exc
    except DownloadClientRequiredError as exc:
        raise ServiceNotConfiguredError("qbittorrent") from exc
    publish_realtime(
        http_request.app,
        ("requests", "queue", "discover"),
        reason="cancel_request" if outcome.settled else "request_withdrawn",
    )
    return WithdrawSubscriptionResponse(settled=outcome.settled)
