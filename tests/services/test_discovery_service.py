"""discovery_service — the tv discover rows (trending_tv / popular_tv) + tile state."""

from __future__ import annotations

import pytest

from plex_manager.ports.metadata import MediaSearchResult
from plex_manager.services import discovery_service
from plex_manager.services.discovery_service import derive_library_state
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
