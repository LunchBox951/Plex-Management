"""auto_grab_service — the unattended request->search->grab worker (ADR-0013).

Covers the honest behaviours the beta depends on: escalating backoff on a
nothing-acceptable search, the strict park-vs-error distinction (a raised Prowlarr
error must NEVER be mistaken for "nothing acceptable"), the per-cycle search cap
that protects the single Prowlarr, the active-download skip, and both the movie and
TV (per-season) grab/park paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.prowlarr import IndexerError
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.models import (
    Download,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.services import auto_grab_service
from plex_manager.services.auto_grab_service import BACKOFF_SCHEDULE, next_search_at
from plex_manager.services.grab_service import GrabError
from tests.web.fakes import (
    FakeProwlarr,
    FakeQbittorrent,
    candidate,
    good_and_cam_candidates,
    prerelease_only_candidates,
)

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


class _RaisingProwlarr:
    """An :class:`IndexerPort` whose search RAISES, like Prowlarr being down."""

    def __init__(self) -> None:
        self.searched: list[IndexerSearchRequest] = []

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        raise IndexerError("prowlarr unreachable")


class _PerTmdbProwlarr:
    """An :class:`IndexerPort` returning a DIFFERENT candidate set per tmdb id.

    Lets a single cycle drive one scope to a GrabError while another grabs cleanly,
    proving one bad grab does not abort the rest of the cycle.
    """

    def __init__(self, by_tmdb: dict[int, list[CandidateRelease]]) -> None:
        self.by_tmdb = by_tmdb
        self.searched: list[IndexerSearchRequest] = []

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        return list(self.by_tmdb.get(request.tmdb_id or 0, []))


def _sourceless_candidate() -> CandidateRelease:
    """A good-quality release the engine ACCEPTS but with NO magnet and NO download
    url -> ``grab_service`` raises :class:`NoGrabSourceError` before anything is ever
    handed to the client (nothing is left live to track)."""
    return CandidateRelease(
        guid="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
        title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
        size_bytes=1_000_000_000,
        magnet_url=None,
        download_url=None,
        info_hash=None,
        seeders=10,
        leechers=1,
        indexer_id=1,
        indexer_name="FakeIndexer",
        publish_date=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _tv_season_pack() -> list[CandidateRelease]:
    """A single acceptable whole-season pack for a TV grab (season 1)."""
    return [candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash="a" * 40)]


async def _seed_movie(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    title: str = "Some Movie",
    year: int | None = 2020,
    status: RequestStatus = RequestStatus.pending,
    search_attempts: int = 0,
    next_search_at: datetime | None = None,
) -> int:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title=title,
            year=year,
            status=status,
            search_attempts=search_attempts,
            next_search_at=next_search_at,
        )
        session.add(request)
        await session.commit()
        return request.id


async def _seed_tv_season(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    season_number: int = 1,
    title: str = "Some Show",
    status: RequestStatus = RequestStatus.pending,
) -> tuple[int, int]:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title=title,
            year=None,
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.flush()
        season = SeasonRequest(
            media_request_id=request.id,
            season_number=season_number,
            status=status,
        )
        session.add(season)
        await session.commit()
        return request.id, season.id


async def _run(
    sessionmaker_: SessionMaker,
    prowlarr: object,
    qbt: FakeQbittorrent,
    *,
    max_searches: int = auto_grab_service.AUTO_GRAB_MAX_SEARCHES_PER_CYCLE,
    now: datetime = _NOW,
) -> auto_grab_service.AutograbCycleResult:
    async with sessionmaker_() as session:
        return await auto_grab_service.run_grab_cycle(
            session,
            prowlarr=prowlarr,  # type: ignore[arg-type]  # a fake IndexerPort
            parser=GuessitParser(),
            profile=default_profile(),
            qbt=qbt,
            max_searches=max_searches,
            now=now,
        )


# --------------------------------------------------------------------------- #
# Backoff ladder
# --------------------------------------------------------------------------- #
def test_next_search_at_backoff_ladder() -> None:
    # rung 0..6 map to the schedule; anything beyond repeats the last (24h) forever.
    for prior in range(len(BACKOFF_SCHEDULE)):
        assert next_search_at(_NOW, prior) == _NOW + BACKOFF_SCHEDULE[prior]
    last = BACKOFF_SCHEDULE[-1]
    assert last == timedelta(hours=24)
    assert next_search_at(_NOW, len(BACKOFF_SCHEDULE)) == _NOW + last
    assert next_search_at(_NOW, 999) == _NOW + last


async def test_no_acceptable_parks_and_schedules_first_backoff(
    sessionmaker_: SessionMaker,
) -> None:
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(prerelease_only_candidates())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.searched == 1
    assert result.no_acceptable == 1
    assert result.grabbed == 0
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.no_acceptable_release
        assert row.search_attempts == 1
        assert row.next_search_at is not None
        assert row.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[0]


async def test_backoff_escalates_from_prior_attempts(sessionmaker_: SessionMaker) -> None:
    # A request that already failed 3 searches (due now) escalates to the 4th rung.
    request_id = await _seed_movie(
        sessionmaker_,
        tmdb_id=603,
        status=RequestStatus.no_acceptable_release,
        search_attempts=3,
        next_search_at=_NOW - timedelta(hours=1),
    )
    prowlarr = FakeProwlarr(prerelease_only_candidates())

    await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.search_attempts == 4
        assert row.next_search_at is not None
        assert row.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[3]


async def test_not_yet_due_request_is_not_searched(sessionmaker_: SessionMaker) -> None:
    # next_search_at in the FUTURE -> the worker leaves it alone this cycle.
    await _seed_movie(
        sessionmaker_,
        tmdb_id=603,
        status=RequestStatus.no_acceptable_release,
        search_attempts=1,
        next_search_at=_NOW + timedelta(hours=1),
    )
    prowlarr = FakeProwlarr(prerelease_only_candidates())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.searched == 0
    assert prowlarr.searched == []


# --------------------------------------------------------------------------- #
# Park vs error — the honesty invariant
# --------------------------------------------------------------------------- #
async def test_search_raise_leaves_state_unchanged_and_propagates(
    sessionmaker_: SessionMaker,
) -> None:
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = _RaisingProwlarr()

    with pytest.raises(IndexerError):
        await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    # Never falsely parked: a Prowlarr outage must not look like "nothing acceptable".
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.pending
        assert row.search_attempts == 0
        assert row.next_search_at is None


# --------------------------------------------------------------------------- #
# Per-cycle search cap
# --------------------------------------------------------------------------- #
async def test_per_cycle_search_cap(sessionmaker_: SessionMaker) -> None:
    for i in range(7):
        await _seed_movie(sessionmaker_, tmdb_id=1000 + i)
    prowlarr = FakeProwlarr(prerelease_only_candidates())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent(), max_searches=5)

    assert result.searched == 5
    assert len(prowlarr.searched) == 5  # only 5 Prowlarr hits despite 7 due
    async with sessionmaker_() as session:
        parked = (
            (
                await session.execute(
                    select(MediaRequest).where(
                        MediaRequest.status == RequestStatus.no_acceptable_release
                    )
                )
            )
            .scalars()
            .all()
        )
        still_pending = (
            (
                await session.execute(
                    select(MediaRequest).where(MediaRequest.status == RequestStatus.pending)
                )
            )
            .scalars()
            .all()
        )
    assert len(parked) == 5
    assert len(still_pending) == 2


# --------------------------------------------------------------------------- #
# Active-download skip
# --------------------------------------------------------------------------- #
async def test_active_download_scope_is_skipped_without_searching(
    sessionmaker_: SessionMaker,
) -> None:
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603, status=RequestStatus.searching)
    async with sessionmaker_() as session:
        session.add(
            Download(
                torrent_hash="deadbeef01",
                status="downloading",
                media_request_id=request_id,
                tmdb_id=603,
            )
        )
        await session.commit()
    prowlarr = FakeProwlarr(good_and_cam_candidates())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.skipped_active == 1
    assert result.searched == 0
    assert prowlarr.searched == []  # never paid for a Prowlarr hit
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.searching  # untouched


# --------------------------------------------------------------------------- #
# Grab success (movie) — the happy path
# --------------------------------------------------------------------------- #
async def test_movie_grab_success_moves_to_downloading(sessionmaker_: SessionMaker) -> None:
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    qbt = FakeQbittorrent()
    prowlarr = FakeProwlarr(good_and_cam_candidates())

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.grabbed == 1
    assert len(qbt.added) == 1  # exactly one torrent handed to the client
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.downloading
        # A successful grab never pushes next_search_at into the future -- so a
        # later failure-rearm to `searching` is immediately due, not backed off.
        assert row.next_search_at is None
        assert row.search_attempts == 0
        download = (
            await session.execute(select(Download).where(Download.media_request_id == request_id))
        ).scalar_one()
        assert download.status == "downloading"


# --------------------------------------------------------------------------- #
# GrabError — operational failure: leave scope unchanged, surface on health,
# NEVER park (a live untracked torrent must not be hidden behind a false park)
# --------------------------------------------------------------------------- #
async def test_grab_error_leaves_scope_unchanged_and_surfaces_on_result(
    sessionmaker_: SessionMaker,
) -> None:
    # A release the decision engine ACCEPTS (good 1080p WEB-DL) that qBittorrent
    # ACCEPTS but for which no info-hash can be derived (no magnet + a download_url
    # the client returns "" for, no indexer info_hash) -> grab_service raises
    # GrabError: a LIVE, untracked torrent with no Download row. This is OPERATIONAL,
    # not "nothing acceptable" -- the scope's state is left COMPLETELY unchanged
    # (never parked) and the error is surfaced on the result for the caller to record
    # on the AutograbStatus health signal.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", magnet=False)])
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 1
    assert result.grabbed == 0
    assert result.no_acceptable == 0  # NOT parked as nothing-acceptable
    assert result.grab_errors == 1
    assert isinstance(result.last_grab_error, GrabError)
    assert len(qbt.added) == 1  # the torrent WAS handed to (accepted by) the client
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        # Left exactly as it was -- never falsely parked, no backoff scheduled.
        assert row.status == RequestStatus.pending
        assert row.search_attempts == 0
        assert row.next_search_at is None
        # No untracked Download row was created for the ungrabbable release.
        downloads = (
            (await session.execute(select(Download).where(Download.media_request_id == request_id)))
            .scalars()
            .all()
        )
        assert downloads == []


async def test_grab_error_does_not_abort_cycle_other_scopes_processed(
    sessionmaker_: SessionMaker,
) -> None:
    # Unlike a raised indexer search (which aborts the whole cycle), a GrabError on
    # one scope must NOT abort the cycle: the torrent reached a reachable qBittorrent,
    # so every remaining due scope is still searched + grabbed. The lower-id scope is
    # processed first, so the GrabError precedes the clean grab.
    bad_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    good_id = await _seed_movie(sessionmaker_, tmdb_id=604)
    prowlarr = _PerTmdbProwlarr(
        {
            603: [candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", magnet=False)],
            604: good_and_cam_candidates(),
        }
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.grab_errors == 1
    assert result.grabbed == 1  # the second scope still grabbed after the bad one
    assert result.searched == 2
    async with sessionmaker_() as session:
        bad = await session.get(MediaRequest, bad_id)
        good = await session.get(MediaRequest, good_id)
        assert bad is not None
        assert good is not None
        assert bad.status == RequestStatus.pending  # untouched
        assert good.status == RequestStatus.downloading  # processed


async def test_no_grab_source_still_parks_on_backoff(sessionmaker_: SessionMaker) -> None:
    # The OTHER accepted-but-ungrabbable cases (here NoGrabSourceError: nothing was
    # ever handed to the client, so nothing is left live to track) still PARK on the
    # escalating backoff -- unlike GrabError they leave no orphan, and left untouched
    # they would re-search Prowlarr every cycle forever.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr([_sourceless_candidate()])
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 1
    assert result.grabbed == 0
    assert result.no_acceptable == 1
    assert result.grab_errors == 0
    assert qbt.added == []  # nothing was ever handed to the client
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.no_acceptable_release
        assert row.search_attempts == 1
        assert row.next_search_at is not None
        assert row.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[0]


# --------------------------------------------------------------------------- #
# TV — per-season park and grab
# --------------------------------------------------------------------------- #
async def test_tv_season_no_acceptable_parks_the_season(sessionmaker_: SessionMaker) -> None:
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1399)
    # Movie-style candidates carry no SxxExx -> rejected WRONG_MEDIA for a season
    # request -> nothing acceptable -> the SEASON is parked (not the parent directly).
    prowlarr = FakeProwlarr(good_and_cam_candidates())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.no_acceptable == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.no_acceptable_release
        assert season.search_attempts == 1
        assert season.next_search_at is not None
        # The parent rollup follows the single season.
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status == RequestStatus.no_acceptable_release


async def test_tv_season_grab_success(sessionmaker_: SessionMaker) -> None:
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1399)
    qbt = FakeQbittorrent()
    prowlarr = FakeProwlarr(_tv_season_pack())

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.grabbed == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status == RequestStatus.downloading
        download = (
            await session.execute(select(Download).where(Download.media_request_id == request_id))
        ).scalar_one()
        assert download.season == 1
