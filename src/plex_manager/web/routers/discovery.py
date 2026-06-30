"""Discovery endpoints — TMDB search + server-composed home. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from plex_manager.ports.metadata import MediaSearchResult, MetadataPort
from plex_manager.services import discovery_service
from plex_manager.services.discovery_service import DiscoverCategory
from plex_manager.web.deps import get_tmdb, require_api_key
from plex_manager.web.schemas import (
    DiscoverHomeResponse,
    DiscoverHomeRow,
    DiscoverListResponse,
    DiscoverResult,
    DiscoverSearchResponse,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/discover",
    tags=["discovery"],
    dependencies=[Depends(require_api_key)],
)


def _to_result(item: MediaSearchResult) -> DiscoverResult:
    """Map a metadata search row to the wire DTO (incl. backdrop for the hero)."""
    return DiscoverResult(
        tmdb_id=item.tmdb_id,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        overview=item.overview,
        poster_url=item.poster_url,
        backdrop_url=item.backdrop_url,
    )


@router.get("/search")
async def discover_search(
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    query: Annotated[str, Query(min_length=1)],
    year: Annotated[int | None, Query()] = None,
) -> DiscoverSearchResponse:
    """Search TMDB for movies / shows matching ``query`` (optional ``year``)."""
    results = await discovery_service.search(tmdb, query, year)
    return DiscoverSearchResponse(results=[_to_result(item) for item in results])


@router.get("/home")
async def discover_home(
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
) -> DiscoverHomeResponse:
    """Return the server-composed Discover home (spotlight + ordered rows)."""
    feed = await discovery_service.home(tmdb)
    return DiscoverHomeResponse(
        spotlight=_to_result(feed.spotlight) if feed.spotlight is not None else None,
        rows=[
            DiscoverHomeRow(
                row_type=row.row_type,
                title=row.title,
                items=[_to_result(item) for item in row.items],
            )
            for row in feed.rows
        ],
    )


@router.get("/{category}")
async def discover_category(
    category: DiscoverCategory,
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> DiscoverListResponse:
    """Return a paginated movie category (``trending`` / ``popular`` / ``upcoming``)."""
    media_page = await discovery_service.list_category(tmdb, category, page)
    return DiscoverListResponse(
        page=media_page.page,
        total_pages=media_page.total_pages,
        total_results=media_page.total_results,
        results=[_to_result(item) for item in media_page.results],
    )
