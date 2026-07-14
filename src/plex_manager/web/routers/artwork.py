"""Plex-native artwork proxy (issue #66) — AUTHENTICATED.

Serves the poster/background image Plex already selected for a LIBRARY item so the
browser can show what the user sees in Plex, WITHOUT the Plex token ever reaching
the frontend. The client names only ``(media_type, tmdb_id, kind)`` — never a Plex
URL — so this endpoint can never be pointed at an arbitrary host (no SSRF): the
adapter resolves the artwork path server-side from Plex's own metadata (GUID match)
and fetches it against the configured Plex origin, injecting the token into the
``X-Plex-Token`` header only. Every not-available path degrades to HTTP 404 so the
browser's ``<img>`` falls back to TMDB artwork (or the gradient), never a 500 and
never a fabricated image.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.ports.library import ArtworkKind, LibraryPort
from plex_manager.ports.metadata import MediaKind
from plex_manager.web.deps import get_library_optional, require_api_key

__all__ = ["router"]

_logger = logging.getLogger(__name__)

# Cache per-browser (``private``): artwork is not user-secret, but the endpoint
# sits behind auth, so a shared/CDN cache must not hold it. A day is plenty — the
# adapter's own short TTL already re-resolves the path, and a changed poster in
# Plex is cosmetic.
_CACHE_CONTROL = "private, max-age=86400"

router = APIRouter(
    prefix="/api/v1/artwork",
    tags=["artwork"],
    dependencies=[Depends(require_api_key)],
)


@router.get(
    "/plex/{media_type}/{tmdb_id}/{kind}",
    responses={
        200: {"content": {"image/*": {}}, "description": "The Plex-native artwork image."},
        404: {"description": "No Plex artwork for this item; the client falls back to TMDB."},
    },
)
async def plex_artwork(
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
    media_type: Annotated[MediaKind, Path()],
    tmdb_id: Annotated[int, Path(ge=1)],
    kind: Annotated[ArtworkKind, Path()],
) -> Response:
    """Proxy a library item's Plex-native ``poster``/``background`` image.

    404 (so the browser falls back to TMDB) whenever Plex is unconfigured, the
    title is not in the library, it has no artwork of that kind, or Plex is
    down/rejects the token — never a 500 that would break the tile.
    """
    if library is None:
        # Plex not configured: no native artwork to serve, fall back to TMDB.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artwork_not_found")
    try:
        image = await library.fetch_artwork(tmdb_id, media_type, kind)
    except (PlexLibraryError, PlexAuthError) as exc:
        # Honesty over silence: a Plex outage/credential failure is logged (no
        # secrets — only the error type), then folded into a 404 so the tile shows
        # TMDB artwork rather than a broken image or a 500. ``NotImplementedError``
        # is deliberately NOT folded in: the port default raises it precisely to
        # fail loudly on a forgotten override (``LibraryPort.fetch_artwork``), so
        # it propagates as a 500 — a programming error, not an artwork miss. The
        # browser's <img> onError falls back to TMDB on a 500 just like a 404, so
        # tiles never break; the operator's logs get the loud failure instead.
        _logger.info(
            "plex artwork unavailable; client falls back to TMDB",
            extra={"error": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artwork_not_found"
        ) from exc
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artwork_not_found")
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={"Cache-Control": _CACHE_CONTROL},
    )
