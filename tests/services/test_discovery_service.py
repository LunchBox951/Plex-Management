"""discovery_service — the tv discover rows (trending_tv / popular_tv)."""

from __future__ import annotations

from plex_manager.ports.metadata import MediaSearchResult
from plex_manager.services import discovery_service
from tests.web.fakes import FakeTmdb


def _show(tmdb_id: int, title: str) -> MediaSearchResult:
    return MediaSearchResult(tmdb_id=tmdb_id, media_type="tv", title=title)


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
