"""discovery_service — the tv discover rows (trending_tv / popular_tv) + tile state."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import pytest

from plex_manager.adapters.tmdb.adapter import TmdbApiError
from plex_manager.ports.metadata import (
    MediaPage,
    MediaSearchResult,
    RecommendationFacet,
    RecommendationProfile,
)
from plex_manager.services import discovery_service
from plex_manager.services.discovery_service import PersonalizationSeed, derive_library_state
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


def _seed(
    tmdb_id: int,
    *,
    media_type: Literal["movie", "tv"] = "movie",
    status: str = "pending",
    is_anime: bool = False,
) -> PersonalizationSeed:
    return PersonalizationSeed(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=f"Seed {tmdb_id}",
        status=status,
        is_anime=is_anime,
    )


def _genre(value_id: int, label: str = "Horror") -> RecommendationFacet:
    return RecommendationFacet(metric="genre", value_id=value_id, label=label)


def _recommendation(
    tmdb_id: int, media_type: Literal["movie", "tv"] = "movie"
) -> MediaSearchResult:
    return MediaSearchResult(tmdb_id=tmdb_id, media_type=media_type, title=f"Rec {tmdb_id}")


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


async def test_personalized_selection_is_stable_for_same_user_and_load_id() -> None:
    seeds = [_seed(index) for index in range(1, 5)]
    profiles: dict[tuple[int, Literal["movie", "tv"]], RecommendationProfile] = {
        (seed.tmdb_id, seed.media_type): RecommendationProfile(facets=(_genre(100 + seed.tmdb_id),))
        for seed in seeds
    }
    recommendations: dict[
        tuple[Literal["movie", "tv"], str, int | None], list[MediaSearchResult]
    ] = {
        ("movie", "genre", 100 + seed.tmdb_id): [_recommendation(1000 + seed.tmdb_id)]
        for seed in seeds
    }
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles=profiles,
        recommendations=recommendations,
    )
    load_id = UUID(int=1)

    first = await discovery_service.home(tmdb, history=seeds, user_id=7, load_id=load_id)
    first_personalized = [row for row in first.rows if row.row_type.startswith("personalized:")]
    calls_after_first = list(tmdb.recommendation_calls)
    second = await discovery_service.home(tmdb, history=seeds, user_id=7, load_id=load_id)
    second_personalized = [row for row in second.rows if row.row_type.startswith("personalized:")]

    assert second_personalized == first_personalized
    assert tmdb.recommendation_calls[len(calls_after_first) :] == calls_after_first
    assert len(first_personalized) == 2
    assert len({row.row_type.rsplit(":", 1)[-1] for row in first_personalized}) == 2


async def test_facetless_history_bounds_profile_fan_out_for_one_load() -> None:
    # Every seed resolves to a facetless profile, so no row is ever built and the
    # loop would otherwise probe the whole history. A single home load must still
    # make at most a constant number of upstream detail calls.
    seeds = [_seed(index) for index in range(1, 40)]
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={
            (seed.tmdb_id, "movie"): RecommendationProfile(facets=()) for seed in seeds
        },
    )

    feed = await discovery_service.home(tmdb, history=seeds, user_id=7, load_id=UUID(int=1))

    assert not any(row.row_type.startswith("personalized:") for row in feed.rows)
    # Bounded to the probe prefix (_PERSONALIZED_ROW_LIMIT * 4 = 8), never the
    # full 39-row history — the amplification the cap exists to prevent.
    assert len(tmdb.recommendation_profile_calls) == 8
    assert len(tmdb.recommendation_profile_calls) < len(seeds)
    assert tmdb.recommendation_calls == []


async def test_different_fixed_load_ids_select_different_seed_sets() -> None:
    seeds = [_seed(index) for index in range(1, 5)]
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={
            (seed.tmdb_id, "movie"): RecommendationProfile(facets=(_genre(100 + seed.tmdb_id),))
            for seed in seeds
        },
        recommendations={
            ("movie", "genre", 100 + seed.tmdb_id): [_recommendation(1000 + seed.tmdb_id)]
            for seed in seeds
        },
    )

    first = await discovery_service.home(tmdb, history=seeds, user_id=7, load_id=UUID(int=1))
    second = await discovery_service.home(tmdb, history=seeds, user_id=7, load_id=UUID(int=2))

    first_types = [row.row_type for row in first.rows if row.row_type.startswith("personalized:")]
    second_types = [row.row_type for row in second.rows if row.row_type.startswith("personalized:")]
    assert first_types != second_types


async def test_metric_then_value_choice_uses_the_stable_prng_stream() -> None:
    seed = _seed(10, is_anime=True)
    facets = (
        _genre(27),
        _genre(28, "Action"),
        RecommendationFacet(metric="director", value_id=1, label="Director"),
        RecommendationFacet(metric="cast", value_id=2, label="Actor"),
        RecommendationFacet(metric="anime", value_id=None, label="anime"),
    )
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={(10, "movie"): RecommendationProfile(facets=facets)},
        recommendations={
            ("movie", "genre", 27): [_recommendation(100)],
            ("movie", "genre", 28): [_recommendation(101)],
            ("movie", "director", 1): [_recommendation(102)],
            ("movie", "cast", 2): [_recommendation(103)],
            ("movie", "anime", None): [_recommendation(104)],
        },
    )

    await discovery_service.home(tmdb, history=[seed], user_id=7, load_id=UUID(int=7))

    assert [(facet.metric, facet.value_id) for _, facet, _ in tmdb.recommendation_calls] == [
        ("genre", 27)
    ]


async def test_tv_seed_rejects_people_facets_and_uses_genre() -> None:
    cast = RecommendationFacet(metric="cast", value_id=2, label="Actor")
    genre = _genre(18, "Drama")
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={(20, "tv"): RecommendationProfile(facets=(cast, genre))},
        recommendations={("tv", "genre", 18): [_recommendation(200, "tv")]},
    )

    feed = await discovery_service.home(
        tmdb, history=[_seed(20, media_type="tv")], user_id=7, load_id=UUID(int=1)
    )

    personalized = [row for row in feed.rows if row.row_type.startswith("personalized:")]
    assert [facet.metric for _, facet, _ in tmdb.recommendation_calls] == ["genre"]
    assert personalized[0].row_type == "personalized:genre:tv:20"


async def test_malformed_value_bearing_anime_facet_is_omitted() -> None:
    malformed = RecommendationFacet(metric="anime", value_id=210024, label="anime")
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={(21, "movie"): RecommendationProfile(facets=(malformed,))},
        recommendations={("movie", "anime", 210024): [_recommendation(201)]},
    )

    feed = await discovery_service.home(
        tmdb,
        history=[_seed(21, is_anime=True)],
        user_id=7,
        load_id=UUID(int=1),
    )

    assert not any(row.row_type.startswith("personalized:") for row in feed.rows)
    assert tmdb.recommendation_calls == []


async def test_completed_anime_request_never_claims_library_membership() -> None:
    # ``completed`` is the in-flight Finalizing state (imported, awaiting Plex
    # confirmation) — see repositories/requests.py. The "is in your library"
    # subtitle must only fire for Plex-verified availability.
    anime = RecommendationFacet(metric="anime", value_id=None, label="anime")
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={(90, "movie"): RecommendationProfile(facets=(anime,))},
        recommendations={("movie", "anime", None): [_recommendation(900)]},
    )

    feed = await discovery_service.home(
        tmdb,
        history=[_seed(90, is_anime=True, status="completed")],
        user_id=7,
        load_id=UUID(int=1),
    )

    personalized = [row for row in feed.rows if row.row_type.startswith("personalized:")]
    assert personalized[0].title == "Because you watch anime"
    assert personalized[0].subtitle == "because you requested Seed 90"


async def test_seed_is_removed_and_successful_rows_compact_into_positions_two_and_four() -> None:
    first_seed = _seed(30)
    second_seed = _seed(40, is_anime=True, status="available")
    anime = RecommendationFacet(metric="anime", value_id=None, label="anime")
    tmdb = FakeTmdb(
        trending=[_recommendation(1)],
        popular=[_recommendation(2)],
        upcoming=[_recommendation(3)],
        trending_tv_results=[_recommendation(4, "tv")],
        popular_tv_results=[_recommendation(5, "tv")],
        recommendation_profiles={
            (30, "movie"): RecommendationProfile(facets=(_genre(27),)),
            (40, "movie"): RecommendationProfile(facets=(anime,)),
        },
        recommendations={
            ("movie", "genre", 27): [
                _recommendation(30),  # the seed itself must never be recommended
                _recommendation(300),
            ],
            ("movie", "anime", None): [_recommendation(400)],
        },
    )

    feed = await discovery_service.home(
        tmdb,
        history=[first_seed, second_seed],
        user_id=7,
        load_id=UUID(int=1),
    )

    assert [row.row_type for row in feed.rows] == [
        "trending",
        "personalized:genre:movie:30",
        "popular",
        "personalized:anime:movie:40",
        "upcoming",
        "trending_tv",
        "popular_tv",
    ]
    personalized = [row for row in feed.rows if row.row_type.startswith("personalized:")]
    assert [item.tmdb_id for row in personalized for item in row.items] == [300, 400]
    assert personalized[1].title == "Because you watch anime"
    assert personalized[1].subtitle == "Seed 40 is in your library"
    assert [item.tmdb_id for item in feed.spotlights] == []  # standard rows lack backdrops


async def test_empty_recommendation_omits_candidate_and_one_success_uses_position_two() -> None:
    tmdb = FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        recommendation_profiles={
            (50, "movie"): RecommendationProfile(facets=(_genre(50),)),
            (60, "movie"): RecommendationProfile(facets=(_genre(60),)),
        },
        recommendations={
            ("movie", "genre", 50): [],
            ("movie", "genre", 60): [_recommendation(600)],
        },
    )

    feed = await discovery_service.home(
        tmdb,
        history=[_seed(50), _seed(60)],
        user_id=7,
        load_id=UUID(int=1),
    )

    assert [row.row_type for row in feed.rows[:3]] == [
        "trending",
        "personalized:genre:movie:60",
        "popular",
    ]
    assert sum(row.row_type.startswith("personalized:") for row in feed.rows) == 1


class _PersonalizationFailureTmdb(FakeTmdb):
    def __init__(self, *, unexpected: bool = False) -> None:
        super().__init__(
            trending=[],
            popular=[],
            upcoming=[],
            recommendation_profiles={(71, "movie"): RecommendationProfile(facets=(_genre(71),))},
            recommendations={("movie", "genre", 71): [_recommendation(710)]},
        )
        self.unexpected = unexpected

    async def recommendation_profile(
        self, tmdb_id: int, media_type: Literal["movie", "tv"]
    ) -> RecommendationProfile | None:
        if tmdb_id == 70:
            if self.unexpected:
                raise RuntimeError("programming error")
            raise TmdbApiError("optional profile unavailable")
        return await super().recommendation_profile(tmdb_id, media_type)


async def test_expected_optional_failure_omits_only_that_candidate() -> None:
    feed = await discovery_service.home(
        _PersonalizationFailureTmdb(),
        history=[_seed(70), _seed(71)],
        user_id=7,
        load_id=UUID(int=1),
    )
    personalized = [row for row in feed.rows if row.row_type.startswith("personalized:")]
    assert [row.row_type for row in personalized] == ["personalized:genre:movie:71"]


async def test_unexpected_optional_failure_propagates() -> None:
    with pytest.raises(RuntimeError, match="programming error"):
        await discovery_service.home(
            _PersonalizationFailureTmdb(unexpected=True),
            history=[_seed(70)],
            user_id=7,
            load_id=UUID(int=1),
        )


async def test_omitted_identity_or_load_id_never_reads_profiles() -> None:
    tmdb = FakeTmdb(trending=[], popular=[], upcoming=[])
    await discovery_service.home(tmdb, history=[_seed(80)], user_id=None, load_id=UUID(int=1))
    await discovery_service.home(tmdb, history=[_seed(80)], user_id=7, load_id=None)
    assert tmdb.recommendation_profile_calls == []


class _AllStandardRowsFailTmdb(FakeTmdb):
    async def trending_movies(self, page: int = 1) -> MediaPage:
        raise TmdbApiError("trending unavailable")

    async def popular_movies(self, page: int = 1) -> MediaPage:
        raise TmdbApiError("popular unavailable")

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        raise TmdbApiError("upcoming unavailable")

    async def trending_tv(self, page: int = 1) -> MediaPage:
        raise TmdbApiError("trending tv unavailable")

    async def popular_tv(self, page: int = 1) -> MediaPage:
        raise TmdbApiError("popular tv unavailable")


async def test_all_standard_row_failures_still_surface_the_upstream_error() -> None:
    with pytest.raises(TmdbApiError, match="trending unavailable"):
        await discovery_service.home(
            _AllStandardRowsFailTmdb(),
            history=[_seed(80)],
            user_id=7,
            load_id=UUID(int=1),
        )


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
