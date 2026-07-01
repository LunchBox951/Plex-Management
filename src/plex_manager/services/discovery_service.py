"""Discovery — TMDB search + server-composed Discover home (movies-first).

``search`` is a thin pass-through. ``home`` composes the home feed SERVER-SIDE
(rows with items embedded) so the frontend renders generically and stays dumb
about why a row exists — TV / recommendation rows are additive later. The row
fetches fan out concurrently; one failing row is logged and returned empty
(honest, retryable), but if EVERY row fails the underlying error is surfaced
rather than a silently blank home.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal, NamedTuple

from plex_manager.adapters.tmdb.adapter import TmdbApiError, TmdbAuthError
from plex_manager.ports.metadata import MediaPage

if TYPE_CHECKING:
    from plex_manager.ports.metadata import MediaSearchResult, MetadataPort

# Upstream failures we tolerate per-row (a flaky/rate-limited TMDB): the row is an
# honest empty row, retryable. ANY other exception is a bug and must surface.
_EXPECTED_ROW_ERRORS = (TmdbApiError, TmdbAuthError)

__all__ = ["DiscoverCategory", "HomeFeed", "HomeRow", "home", "list_category", "search"]

_logger = logging.getLogger(__name__)

DiscoverCategory = Literal["trending", "popular", "upcoming", "trending_tv", "popular_tv"]

# Ordered rows the home composes. Order + titles live here (a code constant, no DB)
# — the recommendation engine that decides rows dynamically is deferred. The tv
# rows are appended after the movie rows (no reordering of the existing three, so
# an established home feed's row order is unchanged); there is no tv "upcoming"
# row -- TMDB has no tv endpoint comparable to its movie release-date listing.
_ROWS: tuple[tuple[DiscoverCategory, str], ...] = (
    ("trending", "Trending this week"),
    ("popular", "Popular movies"),
    ("upcoming", "Coming soon"),
    ("trending_tv", "Trending TV this week"),
    ("popular_tv", "Popular TV shows"),
)


class HomeRow(NamedTuple):
    """One composed row: its open ``row_type``, display ``title``, and items."""

    row_type: str
    title: str
    items: list[MediaSearchResult]


class HomeFeed(NamedTuple):
    """The composed home: an optional spotlight title + the ordered rows."""

    spotlight: MediaSearchResult | None
    rows: list[HomeRow]


async def search(
    tmdb: MetadataPort,
    query: str,
    year: int | None = None,
) -> list[MediaSearchResult]:
    """Return discovery results for ``query`` (optionally constrained to ``year``)."""
    return await tmdb.search(query, year)


async def list_category(
    tmdb: MetadataPort,
    category: DiscoverCategory,
    page: int = 1,
) -> MediaPage:
    """Return one page of a discover category (movie or tv)."""
    if category == "trending":
        return await tmdb.trending_movies(page)
    if category == "popular":
        return await tmdb.popular_movies(page)
    if category == "trending_tv":
        return await tmdb.trending_tv(page)
    if category == "popular_tv":
        return await tmdb.popular_tv(page)
    return await tmdb.upcoming_movies(page)


async def home(tmdb: MetadataPort) -> HomeFeed:
    """Compose the Discover home: fan out page 1 of each row, pick a spotlight."""
    results = await asyncio.gather(
        *(list_category(tmdb, category) for category, _ in _ROWS),
        return_exceptions=True,
    )

    # A genuine programming bug must NOT masquerade as an empty row: re-raise any
    # exception that is not an expected upstream TMDB failure.
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, _EXPECTED_ROW_ERRORS):
            raise result

    # Honesty over silence: if EVERY row failed (e.g. TMDB down), surface the error
    # rather than a silently-empty home. A partial failure logs + empties just that
    # row so the rest of the home still renders.
    if not any(isinstance(result, MediaPage) for result in results):
        for result in results:
            if isinstance(result, BaseException):
                raise result

    rows: list[HomeRow] = []
    spotlight: MediaSearchResult | None = None
    for (category, title), result in zip(_ROWS, results, strict=True):
        if isinstance(result, MediaPage):
            items = result.results
        else:
            # result is an expected TMDB error here; log the actual exception (not
            # just the type) so a flaky row is diagnosable.
            _logger.warning("discover row %r unavailable: %s", category, result)
            items = []
        rows.append(HomeRow(row_type=category, title=title, items=items))
        if spotlight is None:
            spotlight = next((item for item in items if item.backdrop_url), None)
    return HomeFeed(spotlight=spotlight, rows=rows)
