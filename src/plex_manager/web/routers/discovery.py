"""Discovery endpoint — TMDB free-text search. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from plex_manager.ports.metadata import MetadataPort
from plex_manager.services import discovery_service
from plex_manager.web.deps import get_tmdb, require_api_key
from plex_manager.web.schemas import DiscoverResult, DiscoverSearchResponse

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/discover",
    tags=["discovery"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/search")
async def discover_search(
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    query: Annotated[str, Query(min_length=1)],
    year: Annotated[int | None, Query()] = None,
) -> DiscoverSearchResponse:
    """Search TMDB for movies / shows matching ``query`` (optional ``year``)."""
    results = await discovery_service.search(tmdb, query, year)
    return DiscoverSearchResponse(
        results=[
            DiscoverResult(
                tmdb_id=item.tmdb_id,
                media_type=item.media_type,
                title=item.title,
                year=item.year,
                overview=item.overview,
                poster_url=item.poster_url,
            )
            for item in results
        ]
    )
