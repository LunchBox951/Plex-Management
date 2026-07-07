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

# The library-state a Discover/Search tile is decorated with (issue #29). Structurally
# identical to ``web.schemas.LibraryStateField`` -- defined here (not imported) so the
# services layer keeps no web dependency; the two Literals are mutually assignable and
# MUST stay in sync (also with the client's ``lib/tileState.ts`` / ``lib/status.ts``).
LibraryState = Literal["none", "requested", "processing", "available", "partially_available"]

# Request statuses that are "in flight" -- a grab is being worked but the title is not
# yet watchable. Collapsed onto the single ``"processing"`` tile state (the tile is a
# hint, not the request detail; the modal owns the granular lifecycle). Kept in sync
# with ``lib/status.ts``'s intent table and ``RequestStatus`` in ``models.py``.
_PROCESSING_REQUEST_STATUSES: frozenset[str] = frozenset(
    {"searching", "downloading", "completed", "no_acceptable_release", "import_blocked"}
)


def derive_library_state(request_status: str | None, present: bool) -> LibraryState:
    """Fold a request-store status + Plex presence into the tile's base library-state.

    The SERVER base state for a Discover/Search tile: the request status (if the title
    has a request row) drives ``requested``/``processing``/``available``/
    ``partially_available``; a settled-but-not-available status (``failed``/
    ``cancelled``), an unknown status, or NO request row all fall back to Plex presence
    -- ``"available"`` when the title is in the library, else ``"none"``. The presence
    fallback is what flags "owned but never requested through the app", the beta's
    dominant case. Never fabricates presence: absent request + absent from Plex is an
    honest ``"none"``.

    ``evicted`` does NOT fall back to presence: the disk-pressure sweep (ADR-0012)
    just DELETED the file, and ``present`` can still read True -- the warmed presence
    snapshot (``_PRESENT_TMDB_CACHE``, 300s TTL) may predate the eviction, and even
    though ``_evict_one`` triggers a Plex scan + cache invalidation, Plex's refresh is
    asynchronous and keeps reporting the removed item until its scan completes. The
    eviction status is the fresher fact, so it is authoritative: ``"none"``.
    Counter-case, accepted: a long-ago-evicted title manually re-added to Plex outside
    the app loses its tile badge -- acceptable because tiles are hints (the modal and
    the create path read presence fresh, ``use_cache=False``) and "just evicted,
    presence stale" is the common case. This mirrors, and must stay in sync with, the
    client's ``settledBaseFallback`` evicted rule in ``lib/tileState.ts``, which
    applies the same reasoning to the page-load base when the LIVE row settles.

    ``failed``/``cancelled`` genuinely defer to presence: neither settle deletes a
    library file (cancel only acts on not-yet-imported statuses, and ADR-0014
    report-issue purges only while RE-ARMING the row to an ACTIVE status), so presence
    is an independent fact those statuses do not invalidate.

    Kept in sync with ``lib/tileState.ts`` (the client overlays the live request
    lifecycle on top of this base) and ``_SETTLED_REQUEST_STATUSES`` in
    ``repositories/requests.py``.
    """
    if request_status == "pending":
        return "requested"
    if request_status in _PROCESSING_REQUEST_STATUSES:
        return "processing"
    if request_status == "available":
        return "available"
    if request_status == "partially_available":
        return "partially_available"
    if request_status == "evicted":
        # Authoritative over presence -- the sweep just deleted the file; a True
        # ``present`` here is the stale pre-eviction snapshot or Plex's own
        # not-yet-finished scan. See the docstring's evicted paragraph.
        return "none"
    # None, a settled-non-available status (failed/cancelled), or an unknown status:
    # presence is the only honest signal left.
    return "available" if present else "none"


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
    """One composed row: its open ``row_type``, display ``title``, and items.

    ``items`` is an immutable tuple (issue #106): it is sourced directly from
    ``MediaPage.results`` (itself a tuple for the same reason -- the TMDB
    adapter's page cache hands the same object back on every hit within its
    TTL), and a ``NamedTuple`` field being un-reassignable does not stop a
    mutable list held in it from being mutated in place.
    """

    row_type: str
    title: str
    items: tuple[MediaSearchResult, ...]


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
            items = ()
        rows.append(HomeRow(row_type=category, title=title, items=items))
        if spotlight is None:
            spotlight = next((item for item in items if item.backdrop_url), None)
    return HomeFeed(spotlight=spotlight, rows=rows)
