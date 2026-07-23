"""auto_grab_service — the unattended request->search->grab worker (ADR-0013).

Covers the honest behaviours the beta depends on: escalating backoff on a
nothing-acceptable search, the strict park-vs-error distinction (a raised Prowlarr
error must NEVER be mistaken for "nothing acceptable"), the per-cycle search cap
that protects the single Prowlarr, the active-download skip, both the movie and TV
(per-season) grab/park paths, and the bounded try-the-next-accepted-release fall
through (a per-release grab failure on the top pick must not park a still-grabbable
scope behind backoff).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.prowlarr import IndexerError
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest, ScoredRelease
from plex_manager.models import (
    Download,
    DownloadCoverageClaim,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.services import auto_grab_service, log_capture_service
from plex_manager.services.auto_grab_service import (
    BACKOFF_SCHEDULE,
    COOLDOWN_SCHEDULE,
    MAX_GRAB_ATTEMPTS_PER_SCOPE,
    ScopeCooldown,
    cooldown_delay,
    next_search_at,
)
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    GrabError,
    NoGrabSourceError,
)
from tests.web.fakes import (
    FakeProwlarr,
    FakeQbittorrent,
    candidate,
    good_and_cam_candidates,
    prerelease_only_candidates,
)

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
_LOGGER_NAME = "plex_manager.services.auto_grab_service"
# The dedicated telemetry CHILD (wave-6 split): ONLY the issue-#43 records -- the
# enriched source-failure WARNING and the per-cycle summary INFO -- are emitted
# here (INFO-pinned + 30-day-retained); operational records stay on the module
# logger above with ordinary level/retention semantics. ``caplog.at_level`` on
# the MODULE logger still enables the child's records via level inheritance.
_TELEMETRY_LOGGER_NAME = "plex_manager.services.auto_grab_service.telemetry"


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


def _sourceless_candidate(
    title: str = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
    *,
    seeders: int = 10,
) -> CandidateRelease:
    """A good-quality release the engine ACCEPTS but with NO magnet and NO download
    url -> ``grab_service`` raises :class:`NoGrabSourceError` before anything is ever
    handed to the client (nothing is left live to track). ``title``/``seeders`` are
    parametrised so a test can build several distinct sourceless candidates and
    control their rank order (seeders descending)."""
    return CandidateRelease(
        guid=title,
        title=title,
        size_bytes=1_000_000_000,
        magnet_url=None,
        download_url=None,
        info_hash=None,
        seeders=seeders,
        leechers=1,
        indexer_id=1,
        indexer_name="FakeIndexer",
        publish_date=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _two_good_candidates() -> list[CandidateRelease]:
    """Two acceptable, distinct 1080p WEB-DL movie releases, highest-seeded first."""
    return [
        candidate("Some.Movie.2020.1080p.WEB-DL.x264-A", info_hash="a" * 40, seeders=100),
        candidate("Some.Movie.2020.1080p.WEB-DL.x264-B", info_hash="b" * 40, seeders=10),
    ]


def _many_good_candidates(count: int) -> list[CandidateRelease]:
    """``count`` distinct acceptable 1080p WEB-DL movie releases, highest-seeded first."""
    return [
        candidate(f"Some.Movie.2020.1080p.WEB-DL.x264-G{i}", seeders=100 - i) for i in range(count)
    ]


class _ScriptedGrab:
    """Stand-in for ``grab_service.grab`` with a scripted per-call exception sequence.

    Each call records the :class:`ScoredRelease` it was handed -- so a test can assert
    WHICH accepted releases were attempted, and that a settling outcome stopped
    further attempts -- then raises the next scripted exception. Every scripted
    outcome is an exception: a test that needs a SUCCESSFUL grab exercises the real
    ``grab_service.grab`` end-to-end instead. Lets the per-exception loop behaviour
    (clean-skip vs cap-and-park) be driven deterministically, decoupled from how
    ``grab_service`` internally raises each error.
    """

    def __init__(self, *outcomes: Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[ScoredRelease] = []

    async def __call__(
        self,
        qbt: DownloadClientPort,
        session: AsyncSession,
        *,
        scored: ScoredRelease,
        **_kwargs: object,
    ) -> DownloadRecord:
        self.calls.append(scored)
        raise self._outcomes.pop(0)


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
    clock: Callable[[], datetime] | None = None,
    cooldowns: auto_grab_service.CooldownRegistry | None = None,
    save_path: str = "",
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
            # Default the fresh-park clock to the fixed cycle ``now`` so the existing
            # exact-timestamp park assertions stay deterministic; the round-3 fresh-
            # clock test injects an advancing clock instead.
            clock=clock or (lambda: now),
            cooldowns=cooldowns,
            save_path=save_path,
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


async def test_ride_along_coverage_claim_is_skipped_without_searching(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1399, season_number=2)
    async with sessionmaker_() as session:
        pack = Download(
            torrent_hash="ridealong01",
            status="downloading",
            media_request_id=request_id,
            tmdb_id=1399,
            season=1,
        )
        session.add(pack)
        await session.flush()
        session.add(
            DownloadCoverageClaim(
                download_id=pack.id,
                media_request_id=request_id,
                season_number=2,
                status="active",
            )
        )
        await session.commit()
    prowlarr = FakeProwlarr(_tv_season_pack())

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.skipped_active == 1
    assert result.searched == 0
    assert prowlarr.searched == []
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.pending


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
    # Default (no save_path passed): qBittorrent's own default dir stays in
    # charge, unchanged prior behaviour.
    assert qbt.added[0][1] == ""
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


async def test_auto_grab_directs_save_path_when_supplied(sessionmaker_: SessionMaker) -> None:
    """Issues #133/#157: an auto-grabbed torrent is directed at the caller's
    resolved HOST-namespace downloads root exactly like a manual grab, rather
    than landing in qBittorrent's own (possibly invisible) default dir."""
    await _seed_movie(sessionmaker_, tmdb_id=603)
    qbt = FakeQbittorrent()
    prowlarr = FakeProwlarr(good_and_cam_candidates())

    result = await _run(sessionmaker_, prowlarr, qbt, save_path="/home/lunchbox/Downloads")

    assert result.grabbed == 1
    assert len(qbt.added) == 1
    assert qbt.added[0][1] == "/home/lunchbox/Downloads"


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
# Alternate accepted releases — try the next before parking (PR #31)
# --------------------------------------------------------------------------- #
async def test_same_hash_top_pick_attaches_scope_without_client_add(
    sessionmaker_: SessionMaker,
) -> None:
    # The top-ranked accepted release is a multi-season pack whose hash is ALREADY
    # active tracking a DIFFERENT season (season 2). The worker should attach the
    # season-1 scope to the existing physical torrent, not add a duplicate torrent
    # or fall through to a lower-ranked release.
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1399, season_number=1)
    async with sessionmaker_() as session:
        # The shared pack (hash "aaaa...") is already downloading season 2.
        session.add(
            Download(
                torrent_hash="a" * 40,
                status="downloading",
                media_request_id=request_id,
                tmdb_id=1399,
                season=2,
            )
        )
        await session.commit()
    prowlarr = FakeProwlarr(
        [
            candidate("Some.Show.S01.1080p.WEB-DL.x264-PACK", info_hash="a" * 40, seeders=100),
            candidate("Some.Show.S01.1080p.WEB-DL.x264-OTHER", info_hash="b" * 40, seeders=10),
        ]
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 1
    assert result.grabbed == 1
    assert result.no_acceptable == 0
    assert qbt.added == []  # existing same-hash torrent was reused in-place
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        assert season.search_attempts == 0  # no backoff scheduled
        assert season.next_search_at is None
        grabbed_row = (
            await session.execute(select(Download).where(Download.torrent_hash == "a" * 40))
        ).scalar_one()
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == grabbed_row.id)
                )
            )
            .scalars()
            .all()
        )
        assert grabbed_row.status == "downloading"
        assert grabbed_row.season == 2
        assert [(scope.season_number, scope.status) for scope in scopes] == [(1, "active")]


async def test_torrent_tracked_by_other_request_grabs_next_release_cycle_survives(
    sessionmaker_: SessionMaker,
) -> None:
    # Request A's top-ranked accepted release carries a hash ALREADY actively
    # tracked by a DIFFERENT request entirely -> grab_service raises
    # TorrentAlreadyTrackedError at the known-hash precheck (nothing handed to the
    # client; the other request's download owns the physical torrent). That is a
    # PER-RELEASE failure for A: the worker must fall through and GRAB A's
    # lower-ranked release, and the cycle must survive to process request B --
    # pre-fix the error escaped run_grab_cycle and aborted the whole pass.
    other_id = await _seed_movie(
        sessionmaker_, tmdb_id=999, status=RequestStatus.downloading, title="Other Movie"
    )
    a_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    b_id = await _seed_movie(sessionmaker_, tmdb_id=604)
    async with sessionmaker_() as session:
        # The shared hash ("aaaa...") is actively downloading for the OTHER request.
        session.add(
            Download(
                torrent_hash="a" * 40,
                status="downloading",
                media_request_id=other_id,
                tmdb_id=999,
            )
        )
        await session.commit()
    prowlarr = _PerTmdbProwlarr(
        {
            603: [
                candidate("Some.Movie.2020.1080p.WEB-DL.x264-DUPE", info_hash="a" * 40, seeders=99),
                candidate("Some.Movie.2020.1080p.WEB-DL.x264-ALT", info_hash="b" * 40, seeders=10),
            ],
            604: good_and_cam_candidates(),
        }
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 2  # the cycle survived A's duplicate-hash top pick
    assert result.grabbed == 2  # A's fallback release AND B both grabbed
    assert result.no_acceptable == 0
    assert result.grab_errors == 0  # per-release, never an operational error
    assert result.last_grab_error is None
    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, a_id)
        b = await session.get(MediaRequest, b_id)
        assert a is not None and b is not None
        assert a.status == RequestStatus.downloading
        assert b.status == RequestStatus.downloading
        # A's download tracks the FALLBACK hash, never the other request's torrent.
        a_row = (
            await session.execute(select(Download).where(Download.media_request_id == a_id))
        ).scalar_one()
        assert a_row.torrent_hash == "b" * 40
        # The other request's download is untouched.
        other_row = (
            await session.execute(select(Download).where(Download.media_request_id == other_id))
        ).scalar_one()
        assert other_row.torrent_hash == "a" * 40
        assert other_row.status == "downloading"


async def test_all_accepted_ungrabbable_parks_on_backoff(sessionmaker_: SessionMaker) -> None:
    # Every accepted release is sourceless (NoGrabSourceError) -> the list is
    # exhausted with no successful grab, so the scope parks on the escalating backoff
    # exactly as a nothing-acceptable search would (both mean "no grabbable release").
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(
        [
            _sourceless_candidate("Some.Movie.2020.1080p.WEB-DL.x264-A", seeders=100),
            _sourceless_candidate("Some.Movie.2020.1080p.WEB-DL.x264-B", seeders=10),
        ]
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 1
    assert result.grabbed == 0
    assert result.no_acceptable == 1
    assert result.grab_errors == 0
    assert qbt.added == []  # nothing sourceless ever reached the client
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.no_acceptable_release
        assert row.search_attempts == 1
        assert row.next_search_at is not None
        assert row.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[0]


async def test_source_error_parks_scope_on_backoff(sessionmaker_: SessionMaker) -> None:
    # An accepted release whose HTTP source resolves to neither a magnet nor a
    # hashable .torrent -> ``qbt.add`` raises QbittorrentSourceError. qBittorrent is
    # HEALTHY (the SOURCE is unusable) and the raise happens BEFORE the add POST, so
    # NOTHING is left live to track -- a PER-RELEASE failure exactly like
    # NoGrabSourceError, NOT an operational GrabError and NOT a client outage. The
    # sole candidate is unusable, so the list exhausts and the scope PARKS on the
    # escalating backoff (never falsely surfaced as a grab_error / client outage).
    title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr([candidate(title, magnet=False)])
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{title}"})

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 1
    assert result.grabbed == 0
    assert result.no_acceptable == 1
    assert result.grab_errors == 0  # NOT an operational GrabError
    assert result.last_grab_error is None
    assert qbt.added == []  # the raise precedes the add POST -- nothing handed over
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.no_acceptable_release
        assert row.search_attempts == 1
        assert row.next_search_at is not None
        assert row.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[0]


async def test_source_error_does_not_abort_cycle_other_scope_grabs(
    sessionmaker_: SessionMaker,
) -> None:
    # THE regression: pre-fix, a QbittorrentSourceError on scope A's top candidate was
    # caught by NO handler in run_grab_cycle, so it propagated out and aborted the
    # WHOLE pass -- scope B (lower id, but processed after A here) never got searched.
    # A source-unresolvable release is a RELEASE problem, not a client outage: A must
    # park on its own backoff while B is still searched + grabbed cleanly.
    bad_title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    bad_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    good_id = await _seed_movie(sessionmaker_, tmdb_id=604)
    prowlarr = _PerTmdbProwlarr(
        {
            603: [candidate(bad_title, magnet=False)],
            604: good_and_cam_candidates(),
        }
    )
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{bad_title}"})

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.searched == 2  # the cycle survived A and went on to search B
    assert result.grabbed == 1  # B still grabbed after A's unusable source
    assert result.no_acceptable == 1  # A parked on backoff
    assert result.grab_errors == 0  # a per-release failure, never a client outage
    assert result.last_grab_error is None
    async with sessionmaker_() as session:
        bad = await session.get(MediaRequest, bad_id)
        good = await session.get(MediaRequest, good_id)
        assert bad is not None
        assert good is not None
        assert bad.status == RequestStatus.no_acceptable_release  # parked, not aborted
        assert bad.search_attempts == 1
        assert bad.next_search_at is not None
        assert bad.next_search_at.replace(tzinfo=UTC) == _NOW + BACKOFF_SCHEDULE[0]
        assert good.status == RequestStatus.downloading  # processed despite A's failure


async def test_source_error_emits_structured_telemetry(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beta-week telemetry (issue #43): a QbittorrentSourceError WARNING carries
    release identity (source title, indexer, guid/info_hash, season, attempt
    context) via ``extra=`` -- plus request_id/tmdb_id, the correlation keys
    ``log_capture_service`` lifts into ``log_events.context_json`` -- and the
    closing per-cycle summary INFO rolls the count up as ``source_failures``. A
    TV season scope exercises a non-``None`` ``season`` (the release identity the
    beta needs to tell "the same source keeps failing" apart from a generic
    parked state)."""
    title = "Some.Show.S01.1080p.WEB-DL.x264-BAD"
    request_id, _season_request_id = await _seed_tv_season(
        sessionmaker_, tmdb_id=82856, season_number=1
    )
    prowlarr = FakeProwlarr([candidate(title, magnet=False)])
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{title}"})

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.no_acceptable == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "exactly one enriched WARNING for the sole source failure"
    record = warnings[0]
    # The #43 WARNING is telemetry, so it must ride the dedicated child logger
    # (INFO-pinned + 30-day-retained) -- never the operational module logger.
    assert record.name == _TELEMETRY_LOGGER_NAME
    # ``extra=`` sets each key as a plain LogRecord ATTRIBUTE (not a stdlib-known
    # one), so ``getattr`` -- never direct attribute access -- matches this
    # module's other structured-logging tests (e.g. test_request_service.py).
    assert getattr(record, "request_id", None) == request_id
    assert getattr(record, "tmdb_id", None) == 82856
    assert getattr(record, "season", None) == 1
    assert getattr(record, "source_title", None) == title
    assert getattr(record, "indexer", None) == "FakeIndexer"
    assert getattr(record, "guid", None) == title  # ``candidate()`` defaults guid to title
    assert getattr(record, "info_hash", "unset") is None  # no info_hash on this candidate
    assert getattr(record, "attempt", None) == 1
    assert getattr(record, "attempts_total", None) == 1
    # tmdb_id is a correlation key already lifted into log_events' structured
    # context_json, so it need not (and does not) appear in the message text;
    # season/title/indexer/guid are NOT correlation keys, so they must be
    # readable straight off the persisted message text (see the except clause's
    # docstring) or this telemetry's whole reason for existing would not survive
    # into log_events.
    message = record.getMessage()
    assert "82856" not in message  # the tmdb_id, deliberately not duplicated into text
    assert "season=1" in message
    assert f"title={title!r}" in message
    assert "indexer='FakeIndexer'" in message

    summaries = [
        r for r in caplog.records if r.levelname == "INFO" and r.name == _TELEMETRY_LOGGER_NAME
    ]
    assert len(summaries) == 1
    assert getattr(summaries[0], "source_failures", None) == 1
    assert "source_failures=1" in summaries[0].getMessage()


@pytest.mark.parametrize(
    ("uri_guid", "expected_label"),
    [
        # An http(s) private-tracker download URL: passkey in the query.
        ("https://priv.tracker.org/dl/movie?passkey=SUPERSECRETKEY", "priv.tracker.org"),
        # Wave-4 P1: a magnet URI (scheme, NO netloc) -- its percent-encoded
        # ``tr=`` announce parameter embeds the same passkey; the label falls
        # back to the scheme so the class of URI stays diagnosable.
        (
            "magnet:?xt=urn:btih:deadbeef&tr=https%3A%2F%2Fpriv.tracker.org%2Fa%3Fpasskey%3DSUPERSECRETKEY",
            "magnet",
        ),
        # Wave-5 P1: a SCHEMELESS URL parses as pure path (no scheme, no netloc)
        # -- only the allowlist inversion catches it; no host parses, so the
        # token is bare ``#<hash>``.
        ("priv.tracker.org/dl/movie?passkey=SUPERSECRETKEY", ""),
    ],
)
async def test_source_error_redacts_uri_shaped_guid(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    uri_guid: str,
    expected_label: str,
) -> None:
    """Codex P1 (x3): a Prowlarr private-indexer GUID can be a URI of ANY shape
    embedding a tracker passkey/session token -- an http(s) URL in its path/
    query, a magnet URI in its ``tr=`` announce parameters, or a schemeless
    ``host/path?passkey=`` value. The source-unresolvable WARNING persists to
    ``log_events``/``/ops/logs``, so both its message text AND its ``extra=``
    guid field must carry only ``<label>#<sha256-prefix>`` (``safe_guid``'s
    allowlist admits plain ids ONLY) -- the label for diagnosability, the
    credential never emitted (north star #3)."""
    title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    await _seed_movie(sessionmaker_, tmdb_id=603)
    # ``source_errors`` keys on the download_url, so the guid is free to be the URI
    # under test while the qBittorrent source failure is still triggered.
    prowlarr = FakeProwlarr([candidate(title, magnet=False, guid=uri_guid)])
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{title}"})

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.no_acceptable == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    record = warnings[0]

    # The exact redacted token, recomputed INDEPENDENTLY of ``safe_guid`` (so a
    # broken helper cannot vacuously satisfy this test). Exact-token equality --
    # never a bare host-substring check, which CodeQL flags as
    # py/incomplete-url-substring-sanitization ("priv.tracker.org" could sit at
    # an arbitrary position inside an unredacted URL).
    expected_hash = hashlib.sha256(uri_guid.encode("utf-8")).hexdigest()[:12]
    expected_guid = f"{expected_label}#{expected_hash}"

    # ``extra=`` guid: exactly label + hash, secret stripped.
    assert getattr(record, "guid", None) == expected_guid
    assert "SUPERSECRETKEY" not in expected_guid
    assert "passkey" not in expected_guid

    # ...and the message carries exactly that redacted token (``%r``-formatted),
    # while NONE of the secret / query string survives into the persisted text.
    message = record.getMessage()
    assert f"guid={expected_guid!r}" in message
    assert "SUPERSECRETKEY" not in message
    assert "passkey" not in message


async def test_source_error_with_malformed_url_guid_still_parks_and_continues(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex P2 (safe_guid totality): ``urlsplit`` raises ValueError on a
    malformed URL-ish guid (``http://[bad`` -- unclosed IPv6 bracket). Pre-fix
    that throw happened INSIDE the ``QbittorrentSourceError`` handler, escaped
    it, and aborted the ENTIRE grab cycle instead of parking the scope. The
    barrier is now total: the failure is still recorded (WARNING + summary
    rollup), the guid is FULLY redacted to the hash-only ``#<sha256>`` token
    (unparseable means no host to salvage, and the raw text may still embed a
    credential), and the cycle completes normally."""
    title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    malformed_guid = "http://[bad"
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr([candidate(title, magnet=False, guid=malformed_guid)])
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{title}"})

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await _run(sessionmaker_, prowlarr, qbt)  # pre-fix: raised ValueError

    # The cycle survived and the scope parked on backoff -- never aborted.
    assert result.no_acceptable == 1
    assert result.grab_errors == 0
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.no_acceptable_release

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    expected_guid = f"#{hashlib.sha256(malformed_guid.encode('utf-8')).hexdigest()[:12]}"
    assert getattr(warnings[0], "guid", None) == expected_guid
    assert malformed_guid not in warnings[0].getMessage()  # nothing of the raw value

    summaries = [
        r for r in caplog.records if r.levelname == "INFO" and r.name == _TELEMETRY_LOGGER_NAME
    ]
    assert len(summaries) == 1  # the closing summary still ran
    assert getattr(summaries[0], "source_failures", None) == 1


async def test_cycle_summary_reaches_capture_at_warning_operator_floor(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex P2 (log-floor): at ``log_level=WARNING`` the per-cycle summary INFO
    (the issue-#43 ``source_failures`` rollup) used to be filtered at the
    ``.info`` call before any handler ran -- the beta dataset silently never
    persisted. ``configure_logging`` pins the dedicated TELEMETRY CHILD logger
    to INFO (wave-6 split: the operational module logger keeps ordinary
    semantics), so the record is still CREATED and propagates to every root
    handler (the durable ``LogCaptureHandler`` and caplog alike) -- intermediate
    logger levels never filter propagated records -- while ordinary INFO chatter
    from non-pinned loggers stays suppressed."""
    # Drift guard: the pin targets exactly the logger the telemetry emits on.
    assert _TELEMETRY_LOGGER_NAME == log_capture_service.AUTO_GRAB_TELEMETRY_LOGGER_NAME

    await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    qbt = FakeQbittorrent()

    root = logging.getLogger()
    pinned = logging.getLogger(_TELEMETRY_LOGGER_NAME)
    saved_root_level = root.level
    saved_pinned_level = pinned.level
    # WARNING is the exact operator floor that used to drop the summary INFO.
    handler = log_capture_service.configure_logging("WARNING")
    try:
        result = await _run(sessionmaker_, prowlarr, qbt)
        # Control 1: an INFO on a NON-pinned sibling logger must still be dropped
        # by the WARNING floor -- proving the pin (not a permissive root) is what
        # lets the telemetry through.
        logging.getLogger("plex_manager.services.request_service").info("floor control")
        # Control 2: the operational MODULE logger is no longer pinned (wave-6
        # split) -- its INFO records obey the operator floor like any other.
        logging.getLogger(_LOGGER_NAME).info("module-logger control")
    finally:
        log_capture_service.stop_logging(handler)
        root.setLevel(saved_root_level)
        pinned.setLevel(saved_pinned_level)

    assert result.grabbed == 1
    summaries = [
        r
        for r in caplog.records
        if r.name == _TELEMETRY_LOGGER_NAME and r.getMessage().startswith("auto-grab cycle:")
    ]
    assert len(summaries) == 1  # created DESPITE the WARNING floor
    assert getattr(summaries[0], "source_failures", None) == 0
    assert not any(r.getMessage() == "floor control" for r in caplog.records)
    assert not any(r.getMessage() == "module-logger control" for r in caplog.records)


async def test_no_source_errors_emits_zero_source_failures_in_summary(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No-regression: a cycle with no ``QbittorrentSourceError`` at all rolls up
    ``source_failures=0`` in the closing summary INFO -- never fabricated, never
    omitted."""
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    qbt = FakeQbittorrent()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.grabbed == 1
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.downloading

    summaries = [
        r for r in caplog.records if r.levelname == "INFO" and r.name == _TELEMETRY_LOGGER_NAME
    ]
    assert len(summaries) == 1
    assert getattr(summaries[0], "source_failures", None) == 0
    assert "source_failures=0" in summaries[0].getMessage()


async def test_grab_error_on_top_pick_stops_further_attempts(sessionmaker_: SessionMaker) -> None:
    # A GrabError leaves a LIVE, untracked torrent, so the worker must NOT try a
    # second candidate (a live orphan plus another grab would double-download) and
    # must NOT park. The top pick raises GrabError; the grabbable runner-up is left
    # untouched -- proving the operational abort halts the fall-through.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(
        [
            # magnet=False + a download_url the fake client returns "" for -> GrabError.
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-BAD", magnet=False, seeders=100),
            # Would grab cleanly IF it were attempted -- it must not be.
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-GOOD", info_hash="c" * 40, seeders=10),
        ]
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    assert result.grabbed == 0
    assert result.grab_errors == 1
    assert result.no_acceptable == 0  # never parked
    assert isinstance(result.last_grab_error, GrabError)
    assert len(qbt.added) == 1  # only the bad top pick reached the client
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.pending  # left exactly as it was
        assert row.search_attempts == 0
        assert row.next_search_at is None
        # The runner-up ("cccc...") was never grabbed -> no download row exists.
        downloads = (
            (await session.execute(select(Download).where(Download.media_request_id == request_id)))
            .scalars()
            .all()
        )
        assert downloads == []


async def test_already_downloading_is_a_clean_skip(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AlreadyDownloadingError means a download now exists for the scope (a racing
    # manual grab): skip the scope ENTIRELY -- no park, no further candidates, no
    # error -- and leave its state untouched. Two releases are accepted; only the
    # first is attempted, proving the fall-through stops.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(_two_good_candidates())
    scripted = _ScriptedGrab(AlreadyDownloadingError(request_id))
    monkeypatch.setattr(auto_grab_service.grab_service, "grab", scripted)

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.searched == 1
    assert len(scripted.calls) == 1  # no second candidate attempted
    assert result.grabbed == 0
    assert result.no_acceptable == 0  # NOT parked
    assert result.grab_errors == 0  # NOT an operational error
    assert result.last_grab_error is None
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.pending  # untouched
        assert row.search_attempts == 0
        assert row.next_search_at is None


async def test_grab_attempts_capped_then_parks(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # More accepted releases than the cap, EVERY one a per-release failure: the
    # worker attempts exactly MAX_GRAB_ATTEMPTS_PER_SCOPE of them (never the whole
    # list) and then parks on the backoff. Proves the cap bounds grab attempts.
    assert MAX_GRAB_ATTEMPTS_PER_SCOPE == 3
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(_many_good_candidates(MAX_GRAB_ATTEMPTS_PER_SCOPE + 2))
    # Script more failures than the cap so a broken cap surfaces as a call-count
    # mismatch (assertion) rather than an IndexError.
    scripted = _ScriptedGrab(*(NoGrabSourceError(f"guid-{i}") for i in range(5)))
    monkeypatch.setattr(auto_grab_service.grab_service, "grab", scripted)

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert len(scripted.calls) == MAX_GRAB_ATTEMPTS_PER_SCOPE  # capped, not all 5
    assert result.grabbed == 0
    assert result.no_acceptable == 1  # parked after the cap
    assert result.grab_errors == 0
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


# --------------------------------------------------------------------------- #
# Park race — a download appearing before the park write must not false-park
# (Codex PR #31 round-3 #1)
# --------------------------------------------------------------------------- #
async def test_active_download_before_park_skips_the_park(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A nothing-acceptable search would park the scope -- but a racing manual grab
    # lands an active download in the window BETWEEN the pre-search active check and
    # the park write. Parking would overwrite a live ``downloading`` scope with the
    # honest-but-now-wrong ``no_acceptable_release`` dead-end. Drive
    # ``find_active_for_request`` to return None the FIRST time (pre-search: nothing
    # yet) and a live download the SECOND (the round-3 park re-check), exactly the
    # race the manual /queue/grab endpoint guards.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(prerelease_only_candidates())  # nothing acceptable -> would park

    calls = {"n": 0}
    racing = DownloadRecord(
        id=1, torrent_hash="deadbeef01", status="downloading", media_request_id=request_id
    )

    async def _find_active(
        _self: object, _media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else racing

    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository, "find_active_for_request", _find_active
    )

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.searched == 1  # the search DID run (the pre-check saw nothing)
    assert result.no_acceptable == 0  # but the park was SKIPPED -- a download appeared
    assert calls["n"] == 2  # pre-search active check + the round-3 park re-check
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        # No false park: state untouched, no backoff written.
        assert row.status == RequestStatus.pending
        assert row.search_attempts == 0
        assert row.next_search_at is None


async def test_ride_along_coverage_claim_before_park_skips_the_park(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1399, season_number=2)
    prowlarr = FakeProwlarr(good_and_cam_candidates())

    calls = {"n": 0}

    async def _find_active(
        _self: object, _media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        calls["n"] += 1
        return None

    async def _find_coverage_owner(
        _self: object, media_request_id: int, season: int | None
    ) -> DownloadRecord | None:
        if calls["n"] == 2:
            return DownloadRecord(
                id=1,
                torrent_hash="ridealong02",
                status="downloading",
                media_request_id=media_request_id,
            )
        return None

    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository, "find_active_for_request", _find_active
    )
    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository,
        "find_active_coverage_owner",
        _find_coverage_owner,
    )

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert result.searched == 1
    assert result.no_acceptable == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.pending
        assert season.search_attempts == 0
        assert season.next_search_at is None


async def test_park_cas_excludes_coverage_claim_committed_after_the_lookup(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coverage-only claim committed after the read guard must still block parking."""
    _request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=1400, season_number=2)
    prowlarr = FakeProwlarr(prerelease_only_candidates())

    calls = {"n": 0}

    async def _find_active(
        _self: object, _media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        return None

    async def _find_coverage_owner(
        _self: object, media_request_id: int, season: int | None
    ) -> DownloadRecord | None:
        calls["n"] += 1
        if calls["n"] == 2:
            async with sessionmaker_() as competitor:
                pack = Download(
                    torrent_hash="ridealong-race-auto",
                    status="downloading",
                    media_request_id=media_request_id,
                    tmdb_id=1400,
                    season=1,
                )
                competitor.add(pack)
                await competitor.flush()
                competitor.add(
                    DownloadCoverageClaim(
                        download_id=pack.id,
                        media_request_id=media_request_id,
                        season_number=2,
                        status="active",
                    )
                )
                await competitor.commit()
        return None

    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository, "find_active_for_request", _find_active
    )
    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository,
        "find_active_coverage_owner",
        _find_coverage_owner,
    )

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert calls["n"] == 2
    assert result.no_acceptable == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.pending
        assert season.search_attempts == 0
        assert season.next_search_at is None


async def test_park_cas_loses_to_a_concurrent_downloading_write_skips_backoff_too(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #72: the round-3 #1 ``find_active_for_request`` re-check above only
    guards against a racing DOWNLOAD row -- it cannot see a status write landing
    directly on ``MediaRequest.status`` (e.g. a concurrent ``/correction``
    re-grab, or any writer whose ``Download`` row is not yet visible to this
    check). Simulate exactly that: the re-check itself sees nothing (so ``_park``
    proceeds past it), but a genuinely CONCURRENT session flips the request to
    ``downloading`` in the gap immediately after, before ``_park`` reaches the
    actual CAS write. ``mark_no_acceptable_release``'s ``set_status_if_in`` must
    lose that race cleanly -- no status regression -- AND ``_park`` must not have
    already written the backoff (``search_attempts`` / ``next_search_at``) for a
    park that did not happen."""
    request_id = await _seed_movie(sessionmaker_, tmdb_id=605)
    prowlarr = FakeProwlarr(prerelease_only_candidates())  # nothing acceptable -> would park

    calls = {"n": 0}

    async def _find_active_then_race(
        _self: object, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        calls["n"] += 1
        if calls["n"] == 2:
            # The park's own re-check (round-3 #1) sees nothing -- but a
            # genuinely concurrent writer (a SEPARATE session/commit, exactly
            # like the correction-service / eviction-sweep racing tests) lands
            # its status change in the window immediately after this read,
            # before the caller (``_park``) reaches the actual CAS write.
            async with sessionmaker_() as competitor:
                row = await competitor.get(MediaRequest, media_request_id)
                assert row is not None
                row.status = RequestStatus.downloading
                await competitor.commit()
        return None

    monkeypatch.setattr(
        auto_grab_service.SqlDownloadRepository,
        "find_active_for_request",
        _find_active_then_race,
    )

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent())

    assert calls["n"] == 2  # pre-search active check + the round-3 park re-check
    assert result.no_acceptable == 0  # the CAS lost the race -- never counted as parked
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.downloading  # never regressed
        # The acceptance criterion this closes: backoff metadata is not mutated
        # when the scope was not actually parked.
        assert row.search_attempts == 0
        assert row.next_search_at is None


# --------------------------------------------------------------------------- #
# Fresh park clock — each park schedules from its OWN moment (round-3 #3)
# --------------------------------------------------------------------------- #
class _AdvancingClock:
    """A fake clock returning ``start`` then advancing by ``step`` on each call -- so a
    test can prove every PARK reads the clock FRESH (round-3 #3) instead of reusing a
    single cycle-start ``now``."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


async def test_each_park_schedules_from_its_own_fresh_clock_read(
    sessionmaker_: SessionMaker,
) -> None:
    # Two due movies both find nothing acceptable, so both park in one cycle. A slow
    # cycle advances the clock BETWEEN the two parks; each ``next_search_at`` must be
    # one backoff rung from ITS OWN park moment, not from a single cycle-start ``now``
    # (which would schedule the later park in the past -> due again next tick).
    id_a = await _seed_movie(sessionmaker_, tmdb_id=1001)
    id_b = await _seed_movie(sessionmaker_, tmdb_id=1002)
    prowlarr = FakeProwlarr(prerelease_only_candidates())
    step = timedelta(minutes=10)
    clock = _AdvancingClock(_NOW, step)

    result = await _run(sessionmaker_, prowlarr, FakeQbittorrent(), now=_NOW, clock=clock)

    assert result.no_acceptable == 2
    async with sessionmaker_() as session:
        row_a = await session.get(MediaRequest, id_a)
        row_b = await session.get(MediaRequest, id_b)
        assert row_a is not None
        assert row_b is not None
        assert row_a.next_search_at is not None
        assert row_b.next_search_at is not None
        parks = {
            row_a.next_search_at.replace(tzinfo=UTC),
            row_b.next_search_at.replace(tzinfo=UTC),
        }
    # Two DISTINCT park bases -- the first at _NOW, the second ``step`` later. A single
    # cycle-start timestamp would collapse both to ``_NOW + rung`` (one element).
    assert parks == {
        _NOW + BACKOFF_SCHEDULE[0],
        _NOW + step + BACKOFF_SCHEDULE[0],
    }


# --------------------------------------------------------------------------- #
# GrabError cooldown — cool (never park) a scope whose grab keeps failing, so it
# can't starve the per-cycle search budget (round-3 #2)
# --------------------------------------------------------------------------- #
def _grab_error_candidate() -> list[CandidateRelease]:
    """One accepted release qBittorrent takes but yields no info-hash for -> GrabError."""
    return [candidate("Some.Movie.2020.1080p.WEB-DL.x264-BAD", magnet=False)]


def test_cooldown_delay_ladder() -> None:
    assert len(COOLDOWN_SCHEDULE) == 3
    assert COOLDOWN_SCHEDULE[0] == timedelta(minutes=5)
    assert COOLDOWN_SCHEDULE[1] == timedelta(minutes=15)
    assert COOLDOWN_SCHEDULE[2] == timedelta(minutes=60)
    for prior in range(len(COOLDOWN_SCHEDULE)):
        assert cooldown_delay(prior) == COOLDOWN_SCHEDULE[prior]
    # An exhausted ladder repeats the last rung (60m) forever.
    assert cooldown_delay(len(COOLDOWN_SCHEDULE)) == COOLDOWN_SCHEDULE[-1]
    assert cooldown_delay(999) == COOLDOWN_SCHEDULE[-1]


async def test_grab_error_cools_scope_then_skips_it_then_escalates(
    sessionmaker_: SessionMaker,
) -> None:
    # A scope whose grab keeps raising GrabError must be COOLED (never parked -- that
    # would lie: releases exist) and, while cooling, SKIPPED before it costs a search,
    # so it can't consume the budget every tick. Consecutive GrabErrors escalate.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    prowlarr = FakeProwlarr(_grab_error_candidate())
    cooldowns: auto_grab_service.CooldownRegistry = {}
    qbt = FakeQbittorrent()

    # Cycle 1: GrabError -> first cooldown rung (5m); scope left UNCHANGED (not parked).
    r1 = await _run(sessionmaker_, prowlarr, qbt, now=_NOW, cooldowns=cooldowns)
    assert r1.grab_errors == 1
    assert r1.no_acceptable == 0
    assert r1.cooled_down == 1
    assert len(prowlarr.searched) == 1
    # The registry keys by (request_id, season) -- the DB id + season, the same
    # granularity as the active-download guard (NOT the tmdb id).
    scope_key = (request_id, None)
    entry = cooldowns[scope_key]
    assert entry.failures == 1
    assert entry.not_before == _NOW + COOLDOWN_SCHEDULE[0]
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.pending  # cooled, NOT parked
        assert row.search_attempts == 0
        assert row.next_search_at is None

    # Cycle 2, still inside the 5m window: the scope is SKIPPED -- no new search, no
    # budget spent, the cooldown untouched (no escalation while cooling).
    r2 = await _run(
        sessionmaker_, prowlarr, qbt, now=_NOW + timedelta(minutes=2), cooldowns=cooldowns
    )
    assert r2.searched == 0
    assert len(prowlarr.searched) == 1  # unchanged -- no second Prowlarr hit
    assert r2.cooled_down == 1
    assert cooldowns[scope_key].failures == 1

    # Cycle 3, past the window: retried, GrabErrors again -> escalates to the 2nd rung.
    later = _NOW + timedelta(minutes=6)
    r3 = await _run(sessionmaker_, prowlarr, qbt, now=later, cooldowns=cooldowns)
    assert r3.searched == 1
    assert r3.grab_errors == 1
    assert len(prowlarr.searched) == 2
    entry3 = cooldowns[scope_key]
    assert entry3.failures == 2
    assert entry3.not_before == later + COOLDOWN_SCHEDULE[1]


async def test_cooled_scope_yields_search_budget_to_healthy_scope(
    sessionmaker_: SessionMaker,
) -> None:
    # A cooled scope must not consume the per-cycle budget: with room for ONE search
    # and a cooled scope + a healthy scope both due, the search goes to the HEALTHY
    # one (the cooled scope sorts last and never costs a search), which grabs.
    cooled_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    healthy_id = await _seed_movie(sessionmaker_, tmdb_id=604)
    prowlarr = _PerTmdbProwlarr(
        {
            603: _grab_error_candidate(),  # would GrabError if ever searched
            604: good_and_cam_candidates(),  # grabs cleanly
        }
    )
    cooldowns: auto_grab_service.CooldownRegistry = {
        (cooled_id, None): ScopeCooldown(failures=1, not_before=_NOW + timedelta(minutes=5)),
    }
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, now=_NOW, max_searches=1, cooldowns=cooldowns)

    assert result.searched == 1
    searched_tmdbs = [req.tmdb_id for req in prowlarr.searched]
    assert 603 not in searched_tmdbs  # the cooled scope was NOT searched
    assert searched_tmdbs == [604]  # the single search went to the healthy scope
    assert result.grabbed == 1
    async with sessionmaker_() as session:
        cooled = await session.get(MediaRequest, cooled_id)
        healthy = await session.get(MediaRequest, healthy_id)
        assert cooled is not None
        assert healthy is not None
        assert cooled.status == RequestStatus.pending  # untouched
        assert healthy.status == RequestStatus.downloading  # got the budget


async def test_grab_success_clears_the_cooldown(sessionmaker_: SessionMaker) -> None:
    # A scope that recovers (a later grab succeeds) must have its cooldown CLEARED --
    # GrabError is the cooldown's only feeder, so a clean grab starts it fresh.
    request_id = await _seed_movie(sessionmaker_, tmdb_id=603)
    cooldowns: auto_grab_service.CooldownRegistry = {}

    # Cycle 1: GrabError -> cooled.
    bad = FakeProwlarr(_grab_error_candidate())
    r1 = await _run(sessionmaker_, bad, FakeQbittorrent(), now=_NOW, cooldowns=cooldowns)
    assert r1.grab_errors == 1
    assert (request_id, None) in cooldowns

    # Cycle 2 past the window: a grabbable release now exists -> grab -> cooldown cleared.
    good = FakeProwlarr(good_and_cam_candidates())
    later = _NOW + timedelta(minutes=6)
    r2 = await _run(sessionmaker_, good, FakeQbittorrent(), now=later, cooldowns=cooldowns)
    assert r2.grabbed == 1
    assert r2.cooled_down == 0
    assert (request_id, None) not in cooldowns
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status == RequestStatus.downloading


# --------------------------------------------------------------------------- #
# Issue #409 — a multi-season pack must not duplicate already-in-flight seasons
# --------------------------------------------------------------------------- #
class _PerSeasonProwlarr:
    """An :class:`IndexerPort` returning candidates keyed by the queried season.

    Reproduces the Suits scenario: for a per-season query it offers the whole-show
    S01-S09 pack (higher-seeded) plus the single-season pack for exactly that season.
    """

    def __init__(self, by_season: dict[int, list[CandidateRelease]]) -> None:
        self.by_season = by_season
        self.searched: list[IndexerSearchRequest] = []

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        return list(self.by_season.get(request.season or 0, []))


async def _seed_whole_show(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    downloading: range,
    pending: range,
) -> int:
    """Seed a whole-show TV request whose ``downloading`` seasons are already in
    flight and whose ``pending`` seasons are still due to search."""
    all_seasons = sorted({*downloading, *pending})
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Suits",
            status=RequestStatus.downloading,
            tv_request_mode="whole_show",
            requested_seasons_json=list(all_seasons),
        )
        session.add(request)
        await session.flush()
        for n in downloading:
            session.add(
                SeasonRequest(
                    media_request_id=request.id,
                    season_number=n,
                    status=RequestStatus.downloading.value,
                )
            )
        for n in pending:
            session.add(
                SeasonRequest(
                    media_request_id=request.id,
                    season_number=n,
                    status=RequestStatus.pending.value,
                )
            )
        await session.commit()
        return request.id


async def test_multi_season_pack_does_not_duplicate_in_flight_seasons(
    sessionmaker_: SessionMaker,
) -> None:
    # Issue #409 ("Broken Series Logic"), the reported Suits sequence: S1-S7 are
    # already downloading as individual packs; S8-S9 are still pending. When the
    # only release the S8/S9 searches surface is the whole-show S01-S09 pack, the
    # OLD planner accepted it (targeting S8-S9, silently ignoring the S1-S7 it
    # physically re-downloads), so qBittorrent got the redundant pack on top of the
    # seven individual torrents. The pack must now be REJECTED: no S01-S09 torrent
    # is ever added, and with nothing else acceptable the due seasons park honestly
    # on the backoff ladder (they self-heal once a non-overlapping release appears).
    tmdb_id = 5000
    request_id = await _seed_whole_show(
        sessionmaker_, tmdb_id=tmdb_id, downloading=range(1, 8), pending=range(8, 10)
    )
    multipack = candidate(
        "Suits.S01-S09.COMPLETE.1080p.WEB-DL.x264-GROUP", info_hash="e" * 40, seeders=500
    )
    prowlarr = _PerSeasonProwlarr({8: [multipack], 9: [multipack]})
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt)

    # The redundant whole-show pack was never handed to the client (the core defect).
    assert not any("e" * 40 in source for source, _save, _cat in qbt.added)
    assert result.grabbed == 0
    assert result.no_acceptable == 2

    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
    # No download row was created at all -- in particular none for the S01-S09 pack.
    assert downloads == []
    status_by_season = {s.season_number: s.status for s in seasons}
    # The seven in-flight seasons are untouched; the two due seasons park honestly.
    assert all(status_by_season[n] == RequestStatus.downloading.value for n in range(1, 8))
    assert status_by_season[8] == RequestStatus.no_acceptable_release.value
    assert status_by_season[9] == RequestStatus.no_acceptable_release.value
