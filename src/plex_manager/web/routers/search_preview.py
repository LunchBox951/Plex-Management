"""Search-preview endpoint — the headline decision-engine dry run. AUTHENTICATED.

Resolves the media descriptor (from a stored ``request_id`` or the explicit body
fields), runs the indexer search through the pure decision engine, and returns the
ranked accepted releases, the per-release rejection reasons, and the
``no_acceptable_release`` flag. Nothing is grabbed here — this is a preview the FE
renders so the operator can choose.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.decision_engine import DecisionResult
from plex_manager.domain.quality import Resolution
from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.release import ScoredRelease
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.parser import ParserPort
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import decision_service, request_service
from plex_manager.web.deps import (
    get_parser,
    get_prowlarr,
    get_quality_profile,
    get_session,
    require_admin,
)
from plex_manager.web.schemas import (
    AcceptedRelease,
    ErrorDetail,
    RejectedRelease,
    SearchPreviewRequest,
    SearchPreviewResponse,
)

__all__ = ["router", "run_preview"]

router = APIRouter(
    prefix="/api/v1",
    tags=["search-preview"],
    dependencies=[Depends(require_admin)],
)

_SEARCH_PREVIEW_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Request not found"},
    422: {
        "description": "Validation error or missing request descriptor",
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


def _resolution_label(resolution: Resolution) -> str:
    """Human label for a resolution (``1080p``); ``unknown`` for the zero value."""
    return f"{resolution.value}p" if resolution is not Resolution.UNKNOWN else "unknown"


def _to_accepted(scored: ScoredRelease) -> AcceptedRelease:
    candidate = scored.candidate
    return AcceptedRelease(
        title=candidate.title,
        quality_name=scored.quality.name,
        resolution=_resolution_label(scored.quality.resolution),
        source=scored.quality.source.name,
        score=scored.score,
        seeders=candidate.seeders,
        indexer=candidate.indexer_name,
        info_hash=candidate.info_hash,
        guid=candidate.guid,
        covered_seasons=scored.covered_seasons,
        target_seasons=scored.target_seasons,
        upgrade_seasons=scored.upgrade_seasons,
        waste_seasons=scored.waste_seasons,
        ignored_seasons=scored.ignored_seasons,
        skipped_seasons=scored.skipped_seasons,
    )


def _to_response(result: DecisionResult) -> SearchPreviewResponse:
    return SearchPreviewResponse(
        accepted=[_to_accepted(s) for s in result.accepted],
        rejected=[
            RejectedRelease(title=candidate.title, reason=reason.value)
            for candidate, reason in result.rejected
        ],
        no_acceptable_release=result.no_acceptable_release,
    )


async def _resolve_descriptor(
    body: SearchPreviewRequest,
    session: AsyncSession,
) -> tuple[int, str, str, int | None, int | None, list[int] | None]:
    """Return ``(tmdb_id, title, media_type, year, season, episodes)`` for the preview.

    Resolved from a stored request when ``request_id`` is given, else from the
    explicit body fields (which then must be complete). ``season``/``episodes``
    always come from the BODY regardless of the ``request_id`` branch -- a stored
    request carries no per-search season/episode scoping of its own.
    """
    if body.request_id is not None:
        record = await request_service.get_request(session, body.request_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
        return (
            record.tmdb_id,
            record.title,
            record.media_type,
            record.year,
            body.season,
            body.episodes,
        )
    if body.tmdb_id is None or body.media_type is None or body.title is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request_id_or_descriptor_required",
        )
    return body.tmdb_id, body.title, body.media_type, body.year, body.season, body.episodes


async def run_preview(
    body: SearchPreviewRequest,
    session: AsyncSession,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
) -> DecisionResult:
    """Resolve the descriptor and run the decision engine (shared with grab)."""
    tmdb_id, title, media_type, year, season, episodes = await _resolve_descriptor(body, session)
    # Branch on the resolved media's ACTUAL type, never on whether ``season``
    # happens to be set -- mirrors the grab endpoint's exact scope guard
    # (queue.py's tv_grab_requires_season / movie_grab_rejects_season) so an
    # invalid combination is rejected up front, BEFORE the indexer is ever
    # queried: a tv preview with no season would search an unscoped season and
    # return misleading accepted/rejected releases instead of surfacing the
    # invalid request; a movie preview carrying a season/episodes would
    # masquerade as a scoped tv search.
    if media_type == "tv":
        if season is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="tv_grab_requires_season"
            )
    elif season is not None or episodes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="movie_grab_rejects_season"
        )
    return await decision_service.preview(
        prowlarr,
        parser,
        profile,
        SqlBlocklistRepository(session),
        tmdb_id=tmdb_id,
        title=title,
        media_type=media_type,
        year=year,
        season=season,
        episodes=episodes,
    )


@router.post("/search-preview", responses=_SEARCH_PREVIEW_RESPONSES)
async def search_preview_endpoint(
    body: SearchPreviewRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
) -> SearchPreviewResponse:
    """Run the decision engine over the indexer results for this media."""
    result = await run_preview(body, session, prowlarr, parser, profile)
    return _to_response(result)
