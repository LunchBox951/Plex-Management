"""discovery_service — the tv discover rows (trending_tv / popular_tv) + tile state."""

from __future__ import annotations

from typing import Literal

import pytest

from plex_manager.adapters.tmdb.adapter import TmdbApiError
from plex_manager.ports.metadata import MediaPage, MediaSearchResult
from plex_manager.services import discovery_service
from plex_manager.services.discovery_service import derive_library_state
from tests.web.fakes import FakeTmdb


def _show(tmdb_id: int, title: str) -> MediaSearchResult:
    return MediaSearchResult(tmdb_id=tmdb_id, media_type="tv", title=title)


def _hero(
    tmdb_id: int, media_type: Literal["movie", "tv"], *, backdrop: bool = True
) -> MediaSearchResult:
    return MediaSearchResult(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=f"{media_type}-{tmdb_id}",
        backdrop_url=f"https://image/{media_type}-{tmdb_id}.jpg" if backdrop else None,
    )


def _row(row_type: str, *items: MediaSearchResult) -> discovery_service.HomeRow:
    return discovery_service.HomeRow(row_type, row_type, items)


class _CountingFailingTvTmdb(FakeTmdb):
    """Records the normal five home calls while simulating a failed TV pool."""

    def __init__(
        self,
        *,
        trending: list[MediaSearchResult],
        popular: list[MediaSearchResult],
        upcoming: list[MediaSearchResult],
    ) -> None:
        super().__init__(trending=trending, popular=popular, upcoming=upcoming)
        self.discover_calls: list[str] = []

    async def trending_movies(self, page: int = 1) -> MediaPage:
        self.discover_calls.append("trending")
        return await super().trending_movies(page)

    async def popular_movies(self, page: int = 1) -> MediaPage:
        self.discover_calls.append("popular")
        return await super().popular_movies(page)

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        self.discover_calls.append("upcoming")
        return await super().upcoming_movies(page)

    async def trending_tv(self, page: int = 1) -> MediaPage:
        self.discover_calls.append("trending_tv")
        raise TmdbApiError("trending tv unavailable")

    async def popular_tv(self, page: int = 1) -> MediaPage:
        self.discover_calls.append("popular_tv")
        raise TmdbApiError("popular tv unavailable")


async def test_list_category_trending_tv_returns_the_tv_page() -> None:
    tmdb = FakeTmdb(trending_tv_results=[_show(1, "Trending Show")])
    page = await discovery_service.list_category(tmdb, "trending_tv")
    assert [item.title for item in page.results] == ["Trending Show"]


async def test_list_category_popular_tv_returns_the_tv_page() -> None:
    tmdb = FakeTmdb(popular_tv_results=[_show(2, "Popular Show")])
    page = await discovery_service.list_category(tmdb, "popular_tv")
    assert [item.title for item in page.results] == ["Popular Show"]


async def test_home_appends_tv_rows_after_the_movie_rows() -> None:
    # The existing three movie rows keep their order; the tv rows are ADDITIVE,
    # appended after them -- an established home feed's row order is unchanged.
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        trending_tv_results=[_show(1, "Trending Show")],
        popular_tv_results=[_show(2, "Popular Show")],
    )
    feed = await discovery_service.home(tmdb)

    assert [row.row_type for row in feed.rows] == [
        "trending",
        "popular",
        "upcoming",
        "trending_tv",
        "popular_tv",
    ]
    by_type = {row.row_type: row.items for row in feed.rows}
    assert [item.title for item in by_type["trending_tv"]] == ["Trending Show"]
    assert [item.title for item in by_type["popular_tv"]] == ["Popular Show"]


def test_spotlights_take_three_movies_and_three_tv_in_strict_alternation() -> None:
    rows = [
        _row("movies", *(_hero(index, "movie") for index in range(1, 5))),
        _row("shows", *(_hero(index, "tv") for index in range(11, 15))),
    ]

    selected = discovery_service.select_spotlights(rows)

    assert [(item.media_type, item.tmdb_id) for item in selected] == [
        ("movie", 1),
        ("tv", 11),
        ("movie", 2),
        ("tv", 12),
        ("movie", 3),
        ("tv", 13),
    ]
    assert isinstance(selected, tuple)


def test_spotlights_deduplicate_rows_and_skip_missing_backdrops() -> None:
    duplicate = _hero(1, "movie")
    rows = [
        _row("first", duplicate, _hero(2, "movie", backdrop=False), _hero(11, "tv")),
        _row("second", duplicate, _hero(3, "movie"), _hero(11, "tv")),
    ]

    selected = discovery_service.select_spotlights(rows)

    assert [(item.media_type, item.tmdb_id) for item in selected] == [
        ("movie", 1),
        ("tv", 11),
        ("movie", 3),
    ]


def test_spotlights_backfill_a_scarce_media_pool_in_source_order() -> None:
    rows = [
        _row("movies-one", *(_hero(index, "movie") for index in range(1, 5))),
        _row("movies-two", *(_hero(index, "movie") for index in range(5, 8))),
        _row("shows", _hero(11, "tv")),
    ]

    selected = discovery_service.select_spotlights(rows)

    assert [(item.media_type, item.tmdb_id) for item in selected] == [
        ("movie", 1),
        ("tv", 11),
        ("movie", 2),
        ("movie", 3),
        ("movie", 4),
        ("movie", 5),
    ]


def test_spotlights_backfill_when_the_movie_pool_is_empty() -> None:
    rows = [_row("shows", *(_hero(index, "tv") for index in range(11, 18)))]

    selected = discovery_service.select_spotlights(rows)

    assert [item.tmdb_id for item in selected] == [11, 12, 13, 14, 15, 16]


async def test_home_backfills_failed_tv_rows_without_extra_tmdb_calls() -> None:
    tmdb = _CountingFailingTvTmdb(
        trending=[_hero(1, "movie"), _hero(2, "movie")],
        popular=[_hero(3, "movie"), _hero(4, "movie")],
        upcoming=[_hero(5, "movie"), _hero(6, "movie")],
    )

    feed = await discovery_service.home(tmdb)

    assert [item.tmdb_id for item in feed.spotlights] == [1, 2, 3, 4, 5, 6]
    assert tmdb.discover_calls == [
        "trending",
        "popular",
        "upcoming",
        "trending_tv",
        "popular_tv",
    ]


def test_spotlights_return_empty_for_empty_rows() -> None:
    assert discovery_service.select_spotlights([]) == ()
    assert discovery_service.select_spotlights([_row("empty")]) == ()


# --------------------------------------------------------------------------- #
# derive_library_state — the server base tile state (issue #29)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("present", [True, False])
def test_pending_is_requested_regardless_of_presence(present: bool) -> None:
    # A request status always wins over presence (a request row exists), so a
    # pending title reads "requested" whether or not Plex already has (a stale copy of) it.
    assert derive_library_state("pending", present) == "requested"


@pytest.mark.parametrize(
    "status",
    ["searching", "downloading", "completed", "no_acceptable_release", "import_blocked"],
)
@pytest.mark.parametrize("present", [True, False])
def test_in_flight_statuses_collapse_to_processing(status: str, present: bool) -> None:
    assert derive_library_state(status, present) == "processing"


@pytest.mark.parametrize("present", [True, False])
def test_available_request_is_available(present: bool) -> None:
    assert derive_library_state("available", present) == "available"


@pytest.mark.parametrize("present", [True, False])
def test_partially_available_request_is_partial(present: bool) -> None:
    assert derive_library_state("partially_available", present) == "partially_available"


@pytest.mark.parametrize("status", [None, "failed", "cancelled", "totally_unknown"])
def test_no_active_request_falls_back_to_presence(status: str | None) -> None:
    # None (no request row), a settled failed/cancelled status (neither deletes a
    # library file: cancel only acts pre-import, report-issue purges only while
    # re-arming to ACTIVE), or an unrecognised status all defer to Plex presence --
    # "available" when owned, "none" when not. This is the "owned but never requested
    # through the app" path (the beta's dominant case) and the honest neutral for an
    # unknown status; never a fabricated presence.
    assert derive_library_state(status, present=True) == "available"
    assert derive_library_state(status, present=False) == "none"


@pytest.mark.parametrize("present", [True, False])
def test_evicted_is_authoritative_over_presence(present: bool) -> None:
    # ADR-0012 eviction just DELETED the file; a True `present` is the warmed
    # pre-eviction snapshot (or Plex's not-yet-finished scan), so it must not paint
    # a just-evicted title "available". Mirrors the client settledBaseFallback
    # evicted rule in lib/tileState.ts.
    assert derive_library_state("evicted", present) == "none"
