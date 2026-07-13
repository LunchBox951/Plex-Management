"""Discovery endpoints — TMDB search + server-composed home. AUTHENTICATED."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MediaKind, MediaSearchResult, MetadataPort
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import discovery_service
from plex_manager.services.discovery_service import (
    DiscoverCategory,
    LibraryState,
    derive_library_state,
)
from plex_manager.web.deps import (
    AuthContext,
    get_library_optional,
    get_session,
    get_tmdb,
    require_api_key,
)
from plex_manager.web.schemas import (
    DiscoverHomeResponse,
    DiscoverHomeRow,
    DiscoverListResponse,
    DiscoverResult,
    DiscoverSearchResponse,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["router"]

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/discover",
    tags=["discovery"],
    dependencies=[Depends(require_api_key)],
)


def _to_result(item: MediaSearchResult, library_state: LibraryState) -> DiscoverResult:
    """Map a metadata search row to the wire DTO (incl. backdrop for the hero).

    ``library_state`` is the server-computed tile hint (issue #29): the frozen DTO is
    built with it here rather than mutated after, since ``DiscoverResult`` is frozen.
    """
    return DiscoverResult(
        tmdb_id=item.tmdb_id,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        overview=item.overview,
        poster_url=item.poster_url,
        backdrop_url=item.backdrop_url,
        library_state=library_state,
    )


async def _resolve_states(
    session: AsyncSession,
    library: LibraryPort | None,
    items: Iterable[MediaSearchResult],
    auth: AuthContext,
) -> dict[tuple[int, str], LibraryState]:
    """Compute the base library-state for a whole page's tiles in ONE query + ONE crawl.

    Collects the full ``(tmdb_id, media_type)`` key set across ALL supplied items (for
    the home feed: spotlights + every row, so decoration is a single pass, never a
    per-row fan-out) and answers it with one batched request-status lookup plus one
    batched Plex presence read. When Plex is unconfigured (``library`` is ``None``) or
    the crawl fails, presence degrades to empty and the tiles fall back to the
    request-derived state -- an honest missing badge, NEVER a fabricated "not present"
    (the prototype's swallowed-False bug, see ``request_service._already_in_library``).

    Visibility scoping (issue #58): the REQUEST-derived states (Requested /
    Processing / a request-backed available) are scoped for shared sessions
    exactly like ``GET /requests`` — a non-admin only sees badges from their OWN
    request rows, never another user's activity. The Plex PRESENCE bit stays
    GLOBAL for everyone: in-library is physical reality already visible to any
    account browsing Plex itself, not private request activity, so hiding it
    would fabricate a "not owned" (dishonest) while revealing nothing new.
    """
    keys: list[tuple[int, MediaKind]] = [(item.tmdb_id, item.media_type) for item in items]
    if not keys:
        return {}
    repo = SqlRequestRepository(session)
    if auth.is_admin:
        statuses = await repo.display_statuses_by_tmdb_ids(keys)
    elif auth.user_id is not None:
        statuses = await repo.display_statuses_by_tmdb_ids(keys, for_user_id=auth.user_id)
    else:
        # Non-admin with no user identity: unreachable via any current auth method
        # (only Plex sessions yield non-admin contexts, and they always carry a
        # user). Fail CLOSED — no request-derived badges — rather than leak all.
        statuses = {}
    present: frozenset[tuple[int, str]] = frozenset()
    if library is not None:
        try:
            present = await library.present_ids(keys)
        except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
            # Honesty over silence: a Plex failure NEVER 500s Discover and NEVER paints a
            # fake "not owned" -- it drops presence for this page and logs the cause.
            _logger.warning(
                "discover presence decoration unavailable; tiles omit the library badge",
                extra={"error": type(exc).__name__},
            )
    states: dict[tuple[int, str], LibraryState] = {}
    for key in set(keys):
        states[key] = derive_library_state(statuses.get(key), key in present)
    return states


@router.get("/search")
async def discover_search(
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    session: Annotated[AsyncSession, Depends(get_session)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
    auth: Annotated[AuthContext, Depends(require_api_key)],
    query: Annotated[str, Query(min_length=1)],
    year: Annotated[int | None, Query()] = None,
) -> DiscoverSearchResponse:
    """Search TMDB for movies / shows matching ``query`` (optional ``year``)."""
    results = await discovery_service.search(tmdb, query, year)
    states = await _resolve_states(session, library, results, auth)
    return DiscoverSearchResponse(
        results=[_to_result(item, _state_for(states, item)) for item in results]
    )


@router.get("/home")
async def discover_home(
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    session: Annotated[AsyncSession, Depends(get_session)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> DiscoverHomeResponse:
    """Return the server-composed Discover home (spotlights + ordered rows)."""
    feed = await discovery_service.home(tmdb)
    all_items: list[MediaSearchResult] = [
        *feed.spotlights,
        *(item for row in feed.rows for item in row.items),
    ]
    states = await _resolve_states(session, library, all_items, auth)
    return DiscoverHomeResponse(
        spotlights=[_to_result(item, _state_for(states, item)) for item in feed.spotlights],
        rows=[
            DiscoverHomeRow(
                row_type=row.row_type,
                title=row.title,
                items=[_to_result(item, _state_for(states, item)) for item in row.items],
            )
            for row in feed.rows
        ],
    )


@router.get("/{category}")
async def discover_category(
    category: DiscoverCategory,
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    session: Annotated[AsyncSession, Depends(get_session)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
    auth: Annotated[AuthContext, Depends(require_api_key)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> DiscoverListResponse:
    """Return a paginated movie category (``trending`` / ``popular`` / ``upcoming``)."""
    media_page = await discovery_service.list_category(tmdb, category, page)
    states = await _resolve_states(session, library, media_page.results, auth)
    return DiscoverListResponse(
        page=media_page.page,
        total_pages=media_page.total_pages,
        total_results=media_page.total_results,
        results=[_to_result(item, _state_for(states, item)) for item in media_page.results],
    )


def _state_for(
    states: dict[tuple[int, str], LibraryState], item: MediaSearchResult
) -> LibraryState:
    """Look up ``item``'s computed state; ``"none"`` if it was not decorated (degraded)."""
    return states.get((item.tmdb_id, item.media_type), "none")
