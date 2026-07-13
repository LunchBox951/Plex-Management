"""Discovery — TMDB search + server-composed Discover home (movies-first).

``search`` is a thin pass-through. ``home`` composes the home feed SERVER-SIDE
(rows with items embedded) so the frontend renders generically and stays dumb
about why a row exists — including optional personalized rows. The standard row
fetches fan out concurrently; one failing row is logged and returned empty
(honest, retryable), but if EVERY row fails the underlying error is surfaced
rather than a silently blank home.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from typing import TYPE_CHECKING, Literal, NamedTuple
from uuid import UUID

from plex_manager.adapters.tmdb.adapter import TmdbApiError, TmdbAuthError
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.ports.metadata import (
    MediaKind,
    MediaPage,
    RecommendationFacet,
    RecommendationMetric,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plex_manager.ports.metadata import MediaSearchResult, MetadataPort

# Upstream failures we tolerate per-row (a flaky/rate-limited TMDB): the row is an
# honest empty row, retryable. ANY other exception is a bug and must surface.
_EXPECTED_ROW_ERRORS = (TmdbApiError, TmdbAuthError)

__all__ = [
    "DiscoverCategory",
    "HomeFeed",
    "HomeRow",
    "PersonalizationSeed",
    "home",
    "interleave_personalized_rows",
    "list_category",
    "search",
    "select_spotlights",
]

_logger = logging.getLogger(__name__)

_PERSONALIZED_ROW_LIMIT = 2
# Bound the TMDB detail fan-out for ONE home load: without a cap a heavy user
# whose early-shuffled seeds happen to lack usable facets (or transiently fail)
# would drive one uncached ``recommendation_profile`` call per history row before
# giving up. Probing a shuffled prefix keeps worst-case upstream calls constant
# while leaving generous headroom to still fill both rows (nearly every title
# yields at least a genre facet); a fresh load_id reshuffles which seeds a
# subsequent load probes, so the whole history still participates over time.
_PERSONALIZATION_PROBE_LIMIT = _PERSONALIZED_ROW_LIMIT * 4
_METRIC_ORDER: tuple[RecommendationMetric, ...] = ("genre", "director", "cast", "anime")
# A personalized shelf is worth showing only when it offers a genuine "more like
# this" set (issue #277): an obscure seed's facet can round-trip a TMDB discover
# page with just one or two results, which after the self-filter below can leave
# a shelf effectively built from (and standing in for) a single outlier title --
# e.g. a small-budget "Obsession" ending up as the entirety of its own genre
# shelf. Small and deliberately a single named constant so it's easy to find and
# retune; tighten/loosen this ONE value to change the bar for every personalized
# row.
_MIN_SHELF_TITLES = 3
# Statuses that honestly support the "<title> is in your library" subtitle:
# Plex-VERIFIED availability only. ``completed`` is deliberately NOT here — it is
# the in-flight "Finalizing" state (imported, awaiting Plex confirmation; see the
# settled-status commentary in ``repositories/requests.py``), so claiming library
# membership for it would be false until Plex confirms.
_ANIME_LIBRARY_STATUSES: frozenset[str] = frozenset({"available", "partially_available"})

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


# Ordered STANDARD rows the home composes. Order + titles live here (a code
# constant, no DB) -- a FIXED order (issue #278): the home used to interleave
# the (dynamic, per-user) personalized rows into the middle of this list at
# positions two/four, which visually reordered the category shelves from one
# load to the next and made the whole home feel "random". "Coming Soon" is
# deliberately LAST: it is the only row with no tv counterpart (TMDB has no tv
# endpoint comparable to its movie release-date listing), so it anchors the
# end of the feed rather than sitting in the middle of the movie/tv pairs.
# ``interleave_personalized_rows`` below inserts the personalized block right
# before this trailing row -- see its docstring.
_ROWS: tuple[tuple[DiscoverCategory, str], ...] = (
    ("trending", "Trending this week"),
    ("trending_tv", "Trending TV this week"),
    ("popular_tv", "Popular TV"),
    ("popular", "Popular Movies"),
    ("upcoming", "Coming Soon"),
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
    subtitle: str | None = None


class PersonalizationSeed(NamedTuple):
    """Service-local request-history shape; contains no repository/ORM objects."""

    tmdb_id: int
    media_type: MediaKind
    title: str
    status: str
    is_anime: bool


class HomeFeed(NamedTuple):
    """The composed home: immutable spotlight candidates + the ordered rows."""

    spotlights: tuple[MediaSearchResult, ...]
    rows: list[HomeRow]


def select_spotlights(rows: list[HomeRow], limit: int = 6) -> tuple[MediaSearchResult, ...]:
    """Select a balanced, deterministic hero set from the already-fetched rows.

    Candidates retain the server's row/item order, need usable backdrop artwork,
    and are unique by media identity. Up to three movies and three TV shows are
    interleaved movie-first. If either pool is scarce, later candidates from the
    other pool backfill the remaining slots in that pool's original source order.

    This selector is intentionally pure: home composition owns why these titles
    appear, while TMDB is still called exactly once for each normal home row.
    """
    limit = min(limit, 6)
    if limit <= 0:
        return ()

    movies: list[MediaSearchResult] = []
    shows: list[MediaSearchResult] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        for item in row.items:
            key = (item.media_type, item.tmdb_id)
            if not item.backdrop_url or key in seen:
                continue
            seen.add(key)
            if item.media_type == "movie":
                movies.append(item)
            elif item.media_type == "tv":
                shows.append(item)

    selected: list[MediaSearchResult] = []
    for index in range(3):
        if index < len(movies):
            selected.append(movies[index])
        if index < len(shows):
            selected.append(shows[index])

    # Balanced picks above consume at most three per kind. Fill any unused
    # capacity from the abundant pool(s), retaining each pool's source order.
    selected.extend(movies[3:])
    selected.extend(shows[3:])
    return tuple(selected[:limit])


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


def interleave_personalized_rows(
    standard_rows: Sequence[HomeRow], personalized_rows: Sequence[HomeRow]
) -> list[HomeRow]:
    """Insert up to two personalized rows as a FIXED block before the trailing row.

    Issue #278: the prior behavior spliced a personalized row in after every
    other standard row (one-based positions 2/4), which reordered the visible
    category shelves unpredictably from one load to the next and made the whole
    home feel "random". Personalized rows now always land as a single
    contiguous block right before ``standard_rows``' LAST entry (``_ROWS``
    deliberately ends with "Coming Soon" -- see its docstring), giving the
    fixed order: Trending this week, Trending TV this week, Popular TV, Popular
    Movies, [up to two personalized rows], Coming Soon. A successful/failed
    personalized candidate still only affects HOW MANY of the two slots are
    filled, never where they sit.
    """
    personalized = list(personalized_rows[:_PERSONALIZED_ROW_LIMIT])
    if not personalized or not standard_rows:
        return [*standard_rows, *personalized]
    *front, trailing = standard_rows
    return [*front, *personalized, trailing]


def _stable_random(user_id: int, load_id: UUID) -> random.Random:
    """Build a process-independent PRNG from the user/load presentation identity."""
    digest = hashlib.sha256(f"{user_id}\x00{load_id}".encode()).digest()
    # Presentation diversity only, never an authorization/security decision. A
    # local PRNG is required so later choices consume one deterministic stream.
    return random.Random(  # noqa: S311
        int.from_bytes(digest, byteorder="big", signed=False)
    )


def _usable_facet_buckets(
    seed: PersonalizationSeed, facets: Sequence[RecommendationFacet]
) -> dict[RecommendationMetric, list[RecommendationFacet]]:
    buckets: dict[RecommendationMetric, list[RecommendationFacet]] = {}
    for metric in _METRIC_ORDER:
        matching: list[RecommendationFacet] = []
        for facet in facets:
            if facet.metric != metric or not facet.label.strip():
                continue
            if metric == "anime":
                # Anime is the adapter-owned keyword bucket, not a caller-
                # supplied genre/person id. Treat a value-bearing anime facet as
                # malformed so optional personalization omits it instead of
                # silently changing the required discover query semantics.
                if not seed.is_anime or facet.value_id is not None:
                    continue
            elif facet.value_id is None or facet.value_id <= 0:
                continue
            if seed.media_type == "tv" and metric in ("director", "cast"):
                continue
            matching.append(facet)
        if matching:
            buckets[metric] = matching
    return buckets


def _personalized_copy(seed: PersonalizationSeed, facet: RecommendationFacet) -> tuple[str, str]:
    if facet.metric == "genre":
        return f"Because you requested {seed.title}", f"more {facet.label.lower()}"
    if facet.metric == "director":
        return f"Directed by {facet.label}", f"because you requested {seed.title}"
    if facet.metric == "cast":
        return f"Starring {facet.label}", f"because you requested {seed.title}"
    if seed.status in _ANIME_LIBRARY_STATUSES:
        return "Because you watch anime", f"{seed.title} is in your library"
    return "Because you watch anime", f"because you requested {seed.title}"


def _log_optional_failure(
    phase: str, seed: PersonalizationSeed, exc: TmdbApiError | TmdbAuthError
) -> None:
    """Log only bounded ids/types for optional recommendation outages."""
    _logger.warning(
        "personalized discover %s unavailable",
        safe_text(phase),
        extra={
            "tmdb_id": safe_int(seed.tmdb_id),
            "media_type": safe_text(seed.media_type),
            "error": safe_text(type(exc).__name__),
        },
    )


async def _personalized_rows(
    tmdb: MetadataPort,
    history: Sequence[PersonalizationSeed],
    *,
    user_id: int | None,
    load_id: UUID | None,
) -> list[HomeRow]:
    if user_id is None or load_id is None or not history:
        return []

    distinct: list[PersonalizationSeed] = []
    seen: set[tuple[int, MediaKind]] = set()
    for seed in history:
        key = (seed.tmdb_id, seed.media_type)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(seed)

    rng = _stable_random(user_id, load_id)
    rng.shuffle(distinct)
    rows: list[HomeRow] = []
    for seed in distinct[:_PERSONALIZATION_PROBE_LIMIT]:
        if len(rows) >= _PERSONALIZED_ROW_LIMIT:
            break
        try:
            profile = await tmdb.recommendation_profile(seed.tmdb_id, seed.media_type)
        except _EXPECTED_ROW_ERRORS as exc:
            _log_optional_failure("profile", seed, exc)
            continue
        if profile is None:
            continue

        buckets = _usable_facet_buckets(seed, profile.facets)
        if not buckets:
            continue
        metric = rng.choice(list(buckets))
        facet = rng.choice(buckets[metric])
        try:
            page = await tmdb.discover_recommendations(seed.media_type, facet)
        except _EXPECTED_ROW_ERRORS as exc:
            _log_optional_failure("recommendations", seed, exc)
            continue

        items = tuple(
            item
            for item in page.results
            if not (item.tmdb_id == seed.tmdb_id and item.media_type == seed.media_type)
        )
        # Below the minimum, this shelf is effectively just its outlier seed —
        # skip it (never a thin/near-empty personalized row) and let the loop
        # try the next candidate seed instead (issue #277).
        if len(items) < _MIN_SHELF_TITLES:
            continue
        title, subtitle = _personalized_copy(seed, facet)
        rows.append(
            HomeRow(
                row_type=(
                    f"personalized:{facet.metric}:{seed.media_type}:{safe_int(seed.tmdb_id)}"
                ),
                title=title,
                items=items,
                subtitle=subtitle,
            )
        )
    return rows


async def home(
    tmdb: MetadataPort,
    *,
    history: Sequence[PersonalizationSeed] = (),
    user_id: int | None = None,
    load_id: UUID | None = None,
) -> HomeFeed:
    """Compose standard rows plus up to two stable, optional personalized rows."""
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

    standard_rows: list[HomeRow] = []
    for (category, title), result in zip(_ROWS, results, strict=True):
        if isinstance(result, MediaPage):
            items = result.results
        else:
            # result is an expected TMDB error here; log the actual exception (not
            # just the type) so a flaky row is diagnosable.
            _logger.warning("discover row %r unavailable: %s", category, result)
            items = ()
        standard_rows.append(HomeRow(row_type=category, title=title, items=items))

    spotlights = select_spotlights(standard_rows)
    personalized_rows = await _personalized_rows(tmdb, history, user_id=user_id, load_id=load_id)
    return HomeFeed(
        spotlights=spotlights,
        rows=interleave_personalized_rows(standard_rows, personalized_rows),
    )
