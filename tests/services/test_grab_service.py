"""grab_service — terminal-row reuse re-owns to the current request (defensive)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.reconciler import METADATA_STALL_WINDOW
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import AddResult
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.services import grab_service, queue_service
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    GrabError,
    RequestNotActiveError,
    SeasonRequiredError,
    TorrentAlreadyTrackedError,
    TorrentRemovalInFlightError,
)
from plex_manager.services.queue_service import mark_failed
from tests.web.fakes import FakeQbittorrent, candidate

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "a" * 40


def _scored(info_hash: str) -> ScoredRelease:
    cand = candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash=info_hash)
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Movie", source=QualitySource.WEBDL
    )
    return ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )


async def test_grab_reuses_terminal_row_and_reowns_to_current_request(
    sessionmaker_: SessionMaker,
) -> None:
    """A terminal (Failed) download owned by an OLD request, re-grabbed under a NEW
    request, is reused (no UNIQUE collision) AND re-owned to the current request —
    so the active request owns the row, with the stale failure reason cleared."""
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.failed
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="B", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=old_id,
                tmdb_id=100,
                failed_reason="prior failure",
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=new_id,
            tmdb_id=200,
        )
    assert record.status == "downloading"

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        rows = (
            (await session.execute(select(Download).where(Download.torrent_hash == _HASH)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # reused, not duplicated
    assert row.media_request_id == new_id  # re-owned to the CURRENT request
    assert row.tmdb_id == 200  # stale identity refreshed to the CURRENT media
    assert row.failed_reason is None  # stale failure reason cleared


async def test_grab_persists_the_candidates_release_title(sessionmaker_: SessionMaker) -> None:
    """A fresh grab persists ``candidate.title`` as ``Download.release_title`` (issue
    #134) -- the same value already written to ``DownloadHistory.source_title``, so
    the queue can show a human release name without a join into the history log."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=300, media_type=MediaType.movie, title="C", status=RequestStatus.searching
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=request_id,
            tmdb_id=300,
        )
    assert record.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"


async def test_grab_reuse_refreshes_stale_release_title(sessionmaker_: SessionMaker) -> None:
    """A terminal row reused for a fresh grab must not keep the PRIOR grab's release
    name -- a resurrected row showing the old release would mislead the queue about
    which release is actually downloading now (issue #134)."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=400, media_type=MediaType.movie, title="D", status=RequestStatus.searching
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=request_id,
                tmdb_id=400,
                release_title="Old.Stale.Release.2019-GROUP",
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=request_id,
            tmdb_id=400,
        )
    assert record.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"


async def test_grab_reuse_clears_stale_first_seen_at_grace_anchor(
    sessionmaker_: SessionMaker,
) -> None:
    """A terminal row that previously went ClientMissing carries an old
    first_seen_at anchor. Re-grabbing it must reset that anchor to NULL, or the
    reconciler would fast-fail the fresh grab against the long-expired window."""
    stale_anchor = datetime(2020, 1, 1, tzinfo=UTC)
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.searching
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=req_id,
                tmdb_id=100,
                failed_reason="prior failure",
                first_seen_at=stale_anchor,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=req_id,
            tmdb_id=100,
        )

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.status == "downloading"
    assert row.first_seen_at is None  # stale grace anchor cleared on re-grab


async def test_grab_reuse_resets_stale_added_at_stall_anchor(
    sessionmaker_: SessionMaker,
) -> None:
    """A terminal row reused for a fresh grab must not keep the ORIGINAL grab's
    ``added_at`` (issue #165 hardening finding): ``detect_stalls`` anchors its
    metadata/stalled-progress windows on ``added_at``, so a stale value already
    past the stall thresholds would let the very next reconcile tick immediately
    misjudge the brand-new re-grab as stalled -- self-healing (mark-failed +
    remove + blocklist) a download that never had a chance to run."""
    stale_added_at = datetime(2020, 1, 1, tzinfo=UTC)
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.searching
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=req_id,
                tmdb_id=100,
                failed_reason="prior failure",
                added_at=stale_added_at,
            )
        )
        await session.commit()

    before_regrab = datetime.now(UTC)
    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=req_id,
            tmdb_id=100,
        )

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.status == "downloading"
    assert row.added_at is not None
    # SQLite returns a naive datetime even for a DateTime(timezone=True) column;
    # the app always stores UTC (see repositories/downloads.py's ``_as_utc``), so
    # comparing naive-vs-naive here is correct.
    assert row.added_at >= before_regrab.replace(tzinfo=None)


async def test_grab_rejects_terminal_request_and_adds_nothing(
    sessionmaker_: SessionMaker,
) -> None:
    """Grabbing a stale TERMINAL request id (a newer active request owns the media)
    is refused BEFORE anything reaches the client: re-arming the old row would be
    rejected by uq_media_requests_active only after an untracked torrent was added."""
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.completed
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        await session.commit()

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(RequestNotActiveError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=req_id,
                tmdb_id=100,
            )
    # Nothing was handed to the client, and no row was tracked.
    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_rejects_an_evicted_request_and_adds_nothing(
    sessionmaker_: SessionMaker,
) -> None:
    """C2 regression: an ``evicted`` request id (ADR-0012's disk-pressure sweep
    already deleted the file) must be refused BEFORE anything reaches the
    client, exactly like any other terminal status. Before the fix, ``evicted``
    was missing from ``TERMINAL_REQUEST_STATUS_VALUES`` -- a stale client could
    grab an old evicted request id, qbt.add() a torrent, and only then fail
    trying to move this row to ``downloading`` if a FRESH request for the same
    media already owns the ``uq_media_requests_active`` slot, leaving an
    untracked torrent behind."""
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.evicted
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        await session.commit()

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(RequestNotActiveError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=req_id,
                tmdb_id=100,
            )
    # Nothing was handed to the client, and no row was tracked.
    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_raises_when_no_info_hash_can_be_determined(
    sessionmaker_: SessionMaker,
) -> None:
    """qBittorrent accepts an opaque HTTP download_url whose hash cannot be derived
    AND the indexer omitted infoHash: tracking by the guid would make the reconciler
    false-fail it as ClientMissing. Surface a GrabError instead, and persist nothing."""
    cand = candidate("Some.Opaque.Release-GROUP", info_hash=None, magnet=False)
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Opaque Release", source=QualitySource.WEBDL
    )
    scored = ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )

    async with sessionmaker_() as session:
        with pytest.raises(GrabError):
            await grab_service.grab(FakeQbittorrent(), session, scored=scored, tmdb_id=300)

    # Nothing was tracked: no phantom row keyed by the unmatchable guid.
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_reuse_clears_stale_download_path(
    sessionmaker_: SessionMaker,
) -> None:
    """G4: a terminal (Imported) row carries a download_path pointing at the OLD Plex
    library file. Re-grabbing the same hash for a fresh request must clear that
    breadcrumb, or import's _resolve_content would fall back to the stale library path
    and validate the wrong file (block the fresh download as no-video, or wrongly
    complete the new request without importing the new download)."""
    stale_library_path = "/movies/Old Movie (2020)/Old Movie (2020).mkv"
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.completed
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="B", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="imported",
                media_request_id=old_id,
                tmdb_id=100,
                download_path=stale_library_path,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=new_id,
            tmdb_id=200,
        )
    assert record.status == "downloading"

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.media_request_id == new_id  # re-owned to the CURRENT request
    assert row.download_path is None  # stale library breadcrumb cleared on re-grab


def _scored_tv(info_hash: str, title: str) -> ScoredRelease:
    cand = candidate(title, info_hash=info_hash)
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Show", source=QualitySource.WEBDL
    )
    return ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )


async def _make_tv_request(sm: SessionMaker, tmdb_id: int = 900) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(SeasonRequest(media_request_id=request_id, season_number=1, status="pending"))
        session.add(SeasonRequest(media_request_id=request_id, season_number=2, status="pending"))
        await session.commit()
        return request_id


async def test_grab_reuse_resets_stale_progress_and_seed_ratio(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #16: a terminal (Imported) row carries stale progress~1.0 and
    seed_ratio~1.0 from the completed download. Re-grabbing the same hash for a
    fresh request must reset both to 0, or the queue UI shows 100% on a fresh
    grab until the reconciler self-heals (cosmetic, but a 15s blip)."""
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.completed
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="B", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="imported",
                media_request_id=old_id,
                tmdb_id=100,
                progress=1.0,
                seed_ratio=1.0,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=new_id,
            tmdb_id=200,
        )
    assert record.status == "downloading"

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.media_request_id == new_id  # re-owned to the CURRENT request
    assert row.progress == 0.0  # stale progress reset on re-grab
    assert row.seed_ratio == 0.0  # stale seed_ratio reset on re-grab


async def test_grab_tv_persists_season_and_episodes_and_advances_season_rollup(
    sessionmaker_: SessionMaker,
) -> None:
    """A TV grab threads ``season``/``episodes`` onto the Download row and moves
    the OWNING SEASON (not the request directly) to 'downloading' -- the parent's
    computed rollup then reflects that season's transition."""
    request_id = await _make_tv_request(sessionmaker_)

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(_HASH, "Some.Show.S02E05.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[5],
        )
    assert record.status == "downloading"
    assert record.season == 2
    assert record.episodes == [5]

    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        by_season = {row.season_number: row.status.value for row in rows}
        show = await session.get(MediaRequest, request_id)
    assert by_season == {1: "pending", 2: "downloading"}
    assert show is not None
    # Rollup precedence: 'downloading' (season 2) wins outright over 'pending' (season 1).
    assert show.status is RequestStatus.downloading


async def test_grab_allows_concurrent_downloads_for_different_seasons_of_one_show(
    sessionmaker_: SessionMaker,
) -> None:
    """The one-active-download guard is scoped PER SEASON for tv: a whole-series
    request can have season 1 and season 2 downloading at once."""
    request_id = await _make_tv_request(sessionmaker_)
    hash_s1 = "1" * 40
    hash_s2 = "2" * 40

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(hash_s1, "Some.Show.S01.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=1,
        )
    async with sessionmaker_() as session:
        second = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(hash_s2, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
        )
    assert second.status == "downloading"

    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert {(row.season, row.status) for row in rows} == {(1, "downloading"), (2, "downloading")}


async def test_grab_rejects_a_second_release_for_the_same_season(
    sessionmaker_: SessionMaker,
) -> None:
    """Unlike different seasons, a SECOND release for the SAME season still
    collides with the one-active-download-per-season guard."""
    request_id = await _make_tv_request(sessionmaker_)

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv("3" * 40, "Some.Show.S01.720p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=1,
        )
    async with sessionmaker_() as session:
        with pytest.raises(AlreadyDownloadingError):
            await grab_service.grab(
                FakeQbittorrent(),
                session,
                scored=_scored_tv("4" * 40, "Some.Show.S01.1080p.WEB-DL.x264-GROUP"),
                request_id=request_id,
                tmdb_id=900,
                season=1,
            )


async def test_grab_rejects_pack_when_target_sibling_has_different_active_release(
    sessionmaker_: SessionMaker,
) -> None:
    """A multi-season pack claims every planned target season. If any sibling season
    already has a different active torrent, the pack is rejected before qBittorrent is
    asked to add it."""
    request_id = await _make_tv_request(sessionmaker_)

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv("5" * 40, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
        )

    pack = _scored_tv("6" * 40, "Some.Show.S01-S02.1080p.WEB-DL.x264-GROUP").model_copy(
        update={"target_seasons": (1, 2)}
    )
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(AlreadyDownloadingError):
            await grab_service.grab(
                qbt,
                session,
                scored=pack,
                request_id=request_id,
                tmdb_id=900,
                season=1,
            )

    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert len(rows) == 1
    assert rows[0].torrent_hash == "5" * 40


async def test_grab_attaches_same_hash_active_for_a_different_season(
    sessionmaker_: SessionMaker,
) -> None:
    """A multi-season pack (one hash) already downloading for season 1, grabbed again
    for season 2, attaches season 2 as another logical scope on the same physical
    Download row instead of silently treating it as an idempotent no-op."""
    request_id = await _make_tv_request(sessionmaker_)
    pack_hash = "7" * 40

    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(pack_hash, "Some.Show.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=1,
        )
    assert first.season == 1

    async with sessionmaker_() as session:
        second = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(pack_hash, "Some.Show.S01-S03.COMPLETE.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
        )
    assert second.id == first.id

    # One physical row now carries both TV scopes.
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.torrent_hash == pack_hash)))
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == rows[0].id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].season == 1
    assert sorted(scope.season_number for scope in scopes if scope.season_number is not None) == [
        1,
        2,
    ]


async def test_grab_fresh_multi_season_pack_returns_all_attached_scopes(
    sessionmaker_: SessionMaker,
) -> None:
    """A fresh accepted multi-season pack attaches every planned season in the same
    transaction and returns the refreshed record with those scopes populated."""
    request_id = await _make_tv_request(sessionmaker_)
    pack_hash = "a" * 40
    scored = _scored_tv(pack_hash, "Some.Show.S01-S02.COMPLETE.1080p.WEB-DL.x264-GROUP").model_copy(
        update={"target_seasons": (1, 2)}
    )

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=scored,
            request_id=request_id,
            tmdb_id=900,
            season=1,
        )

    assert record.status == "downloading"
    assert [scope.season for scope in record.scopes] == [1, 2]
    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == record.id)
                )
            )
            .scalars()
            .all()
        )

    assert {season.season_number: season.status for season in seasons} == {
        1: "downloading",
        2: "downloading",
    }
    assert sorted(scope.season_number for scope in scopes if scope.season_number is not None) == [
        1,
        2,
    ]


async def test_grab_fresh_multi_season_pack_preserves_sibling_explicit_episode_scopes(
    sessionmaker_: SessionMaker,
) -> None:
    """A multi-season pack accepted for an episode-scoped request must attach each
    sibling season with that season's requested episodes, not widen siblings to
    whole-season scopes."""
    request_id = await _make_tv_request(sessionmaker_)
    pack_hash = "b" * 40
    scored = _scored_tv(pack_hash, "Some.Show.S01-S02.COMPLETE.1080p.WEB-DL.x264-GROUP").model_copy(
        update={"target_seasons": (1, 2)}
    )

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=scored,
            request_id=request_id,
            tmdb_id=900,
            season=1,
            episodes=[5],
            scope_episodes_by_season={1: [5], 2: [6]},
        )

    async with sessionmaker_() as session:
        scopes = (
            (
                await session.execute(
                    select(DownloadScope)
                    .where(DownloadScope.download_id == record.id)
                    .order_by(DownloadScope.season_number)
                )
            )
            .scalars()
            .all()
        )

    assert [(scope.season_number, scope.episodes_json) for scope in scopes] == [(1, [5]), (2, [6])]


async def test_grab_attaches_same_hash_active_for_uncovered_episodes(
    sessionmaker_: SessionMaker,
) -> None:
    """Same-hash reuse compares the full scope, not just the season: an active row
    scoped to S02 episode [4], re-grabbed for the SAME hash + season but an
    uncovered episode [5], attaches an additional logical scope. A covered request
    (the same [4], or a subset) stays an idempotent no-op."""
    request_id = await _make_tv_request(sessionmaker_)
    h = "8" * 40
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02E04.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[4],
        )
    assert first.episodes == [4]

    async with sessionmaker_() as session:
        second = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02E05.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[5],
        )
    assert second.id == first.id

    # The already-requested episode [4] is COVERED -> idempotent no-op, same row.
    async with sessionmaker_() as session:
        again = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02E04.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[4],
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == first.id)
                )
            )
            .scalars()
            .all()
        )
    assert again.id == first.id
    assert sorted(scope.episodes_json for scope in scopes if scope.episodes_json is not None) == [
        [4],
        [5],
    ]


async def test_grab_tv_request_missing_season_raises_season_required(
    sessionmaker_: SessionMaker,
) -> None:
    """F1 (defense in depth): a tv request grabbed with no season is refused
    BEFORE anything reaches the client -- the domain-boundary backstop holds
    even if a future caller bypasses the endpoint's own 422 guard, so an
    unscoped tv download (which would update the parent MediaRequest directly
    instead of a SeasonRequest) can never be persisted."""
    request_id = await _make_tv_request(sessionmaker_)
    qbt = FakeQbittorrent()

    async with sessionmaker_() as session:
        with pytest.raises(SeasonRequiredError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored_tv("5" * 40, "Some.Show.S01.1080p.WEB-DL.x264-GROUP"),
                request_id=request_id,
                tmdb_id=900,
                season=None,
            )
    # Nothing was handed to the client, and no row was tracked.
    assert qbt.added == []
    async with sessionmaker_() as session:
        rows = (await session.execute(select(Download))).scalars().all()
    assert rows == []


async def test_grab_movie_with_season_is_coerced_and_still_enforces_one_active_guard(
    sessionmaker_: SessionMaker,
) -> None:
    """F6: a movie grab carrying a (bogus, caller-supplied) ``season`` is coerced
    back to ``None`` rather than trusted -- so it can never spawn a
    ``SeasonRequest`` row, and the one-active-download guard is never bypassed
    by branching on ``season is not None`` instead of the request's ACTUAL media
    type: a second release tagged with a DIFFERENT bogus season must still
    collide with the (season-agnostic-for-movies) guard, not slip through as a
    "different season"."""
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.searching
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        await session.commit()

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            qbt,
            session,
            scored=_scored("1" * 40),
            request_id=req_id,
            tmdb_id=100,
            season=7,  # bogus -- must be coerced to None, never trusted
        )
    assert first.status == "downloading"
    assert first.season is None

    # A DIFFERENT release for the SAME movie, tagged with a DIFFERENT bogus
    # season: if season were trusted instead of coerced, the one-active guard's
    # find_active_for_request(season=9) would miss the season=None row above and
    # wrongly let a second active download through.
    async with sessionmaker_() as session:
        with pytest.raises(AlreadyDownloadingError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored("2" * 40),
                request_id=req_id,
                tmdb_id=100,
                season=9,
            )

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.media_request_id == req_id)))
            .scalars()
            .all()
        )
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == req_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].season is None
    assert seasons == []  # no SeasonRequest ever spawned for a movie


async def test_grab_reuse_refreshes_tv_scope_to_current_grab(
    sessionmaker_: SessionMaker,
) -> None:
    """F2: a terminal (Imported) row for a torrent hash previously scoped to
    season=1/episodes=None must have its TV scope REFRESHED when the SAME hash
    is re-selected for a DIFFERENT season (e.g. a multi-season pack) -- not
    silently keep serving the stale scope while the newly requested season is
    marked downloading."""
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.tv, title="Old Show", status=RequestStatus.completed
        )
        new = MediaRequest(
            tmdb_id=900, media_type=MediaType.tv, title="Some Show", status=RequestStatus.pending
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        session.add(SeasonRequest(media_request_id=new_id, season_number=2, status="pending"))
        session.add(
            Download(
                torrent_hash=_HASH,
                status="imported",
                media_request_id=old_id,
                tmdb_id=100,
                season=1,
                episodes_json=None,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(_HASH, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=new_id,
            tmdb_id=900,
            season=2,
            episodes=[3, 4],
        )
    assert record.status == "downloading"
    assert record.season == 2
    assert record.episodes == [3, 4]
    assert record.media_request_id == new_id  # re-owned to the CURRENT request

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert row.season == 2
    assert row.episodes_json == [3, 4]
    assert row.media_request_id == new_id


async def test_grab_reuse_refreshes_metadata_used_by_blocklist(
    sessionmaker_: SessionMaker,
) -> None:
    """A terminal row reused for a new request must not keep the old tmdb/year/season
    identity, or a later mark-failed blocklists the wrong media item."""
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="Old", status=RequestStatus.completed
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="New", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=old.id,
                tmdb_id=100,
                year=1990,
                season=1,
                magnet_link="magnet:?xt=urn:btih:old",
            )
        )
        await session.commit()
        new_id = new.id

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=new_id,
            tmdb_id=200,
            year=2024,
            season=2,
        )

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        assert row.tmdb_id == 200
        assert row.year == 2024
        # The request is a movie, so the merged grab invariant coerces caller
        # season/episodes back to NULL while still refreshing the row's identity.
        assert row.season is None
        assert row.media_type == MediaType.movie
        assert row.magnet_link == f"magnet:?xt=urn:btih:{_HASH}"
        await mark_failed(session, FakeQbittorrent(), download_id=row.id, blocklist=True)

    async with sessionmaker_() as session:
        entry = (await session.execute(select(Blocklist))).scalar_one()
    assert entry.tmdb_id == 200
    assert entry.media_type == MediaType.movie


async def test_grab_reuse_refreshes_episodes_for_same_season_regrab(
    sessionmaker_: SessionMaker,
) -> None:
    """F2 (second case): re-selecting the SAME hash for the SAME season but a
    DIFFERENT episode filter must also rewrite ``episodes_json``, not keep
    serving the prior grab's episode list."""
    request_id = await _make_tv_request(sessionmaker_)
    hash_ = "6" * 40

    async with sessionmaker_() as session:
        await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(hash_, "Some.Show.S01E01E02E03.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=1,
            episodes=[1, 2, 3],
        )
    # Fail it (not blocklisted) so it becomes a terminal row eligible for reuse.
    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == hash_))
        ).scalar_one()
        row.status = "failed"
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(hash_, "Some.Show.S01E04E05.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=1,
            episodes=[4, 5],
        )
    assert record.status == "downloading"
    assert record.episodes == [4, 5]

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == hash_))
        ).scalar_one()
    assert row.episodes_json == [4, 5]


async def test_grab_rejects_same_active_hash_owned_by_another_request_precheck(
    sessionmaker_: SessionMaker,
) -> None:
    """Same-hash idempotency is only valid for the same request. Returning another
    request's active row would leave the current request unchanged while reporting
    success."""
    async with sessionmaker_() as session:
        owner = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="Owner", status=RequestStatus.downloading
        )
        current = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="Current", status=RequestStatus.searching
        )
        session.add_all([owner, current])
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=owner.id,
                tmdb_id=100,
            )
        )
        await session.commit()
        current_id = current.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(TorrentAlreadyTrackedError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=current_id,
                tmdb_id=200,
            )

    assert qbt.added == []  # rejected before handing anything to qBittorrent
    async with sessionmaker_() as session:
        current = await session.get(MediaRequest, current_id)
        assert current is not None and current.status == RequestStatus.searching
        assert (await session.execute(select(DownloadHistory))).scalars().all() == []


class _HashReturningQbt(FakeQbittorrent):
    def __init__(self, info_hash: str) -> None:
        super().__init__()
        self._info_hash = info_hash

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        self.added.append((magnet_or_url, save_path, category))
        return AddResult(torrent_hash=self._info_hash, created=True)


async def test_grab_rejects_same_active_hash_owned_by_another_request_after_add(
    sessionmaker_: SessionMaker,
) -> None:
    """The same ownership check also applies when the hash is only known after
    qBittorrent returns it."""
    async with sessionmaker_() as session:
        owner = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="Owner", status=RequestStatus.downloading
        )
        current = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="Current", status=RequestStatus.searching
        )
        session.add_all([owner, current])
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=owner.id,
                tmdb_id=100,
            )
        )
        await session.commit()
        current_id = current.id

    cand = candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash=None, magnet=True)
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Movie", source=QualitySource.WEBDL
    )
    scored = ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )
    qbt = _HashReturningQbt(_HASH)
    async with sessionmaker_() as session:
        with pytest.raises(TorrentAlreadyTrackedError):
            await grab_service.grab(
                qbt,
                session,
                scored=scored,
                request_id=current_id,
                tmdb_id=200,
            )

    assert qbt.added != []
    async with sessionmaker_() as session:
        current = await session.get(MediaRequest, current_id)
        assert current is not None and current.status == RequestStatus.searching
        assert (await session.execute(select(DownloadHistory))).scalars().all() == []


class _CompetingActiveDuringAddQbt(FakeQbittorrent):
    def __init__(self, sessionmaker_: SessionMaker, request_id: int, info_hash: str) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker_
        self._request_id = request_id
        self._info_hash = info_hash

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        self.added.append((magnet_or_url, save_path, category))
        async with self._sessionmaker() as session:
            session.add(
                Download(
                    torrent_hash="b" * 40,
                    status="downloading",
                    media_request_id=self._request_id,
                    tmdb_id=999,
                )
            )
            await session.commit()
        return AddResult(torrent_hash=self._info_hash, created=True)


class _CompetingHashOwnerDuringAddQbt(FakeQbittorrent):
    def __init__(self, sessionmaker_: SessionMaker, owner_request_id: int, info_hash: str) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker_
        self._owner_request_id = owner_request_id
        self._info_hash = info_hash

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        self.added.append((magnet_or_url, save_path, category))
        async with self._sessionmaker() as session:
            session.add(
                Download(
                    torrent_hash=self._info_hash,
                    status="downloading",
                    media_request_id=self._owner_request_id,
                    tmdb_id=100,
                )
            )
            await session.commit()
        return AddResult(torrent_hash=self._info_hash, created=True)


async def test_grab_insert_conflict_rejects_same_hash_owned_by_another_request(
    sessionmaker_: SessionMaker,
) -> None:
    """If another request wins the same torrent_hash UNIQUE race, recovery must
    reject ownership transfer instead of returning the other request's active row
    as if this request had grabbed successfully."""
    async with sessionmaker_() as session:
        owner = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="Owner", status=RequestStatus.downloading
        )
        current = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="Current", status=RequestStatus.searching
        )
        session.add_all([owner, current])
        await session.flush()
        owner_id, current_id = owner.id, current.id
        await session.commit()

    qbt = _CompetingHashOwnerDuringAddQbt(sessionmaker_, owner_id, _HASH)
    async with sessionmaker_() as session:
        with pytest.raises(TorrentAlreadyTrackedError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=current_id,
                tmdb_id=200,
            )

    assert qbt.added != []
    async with sessionmaker_() as session:
        current = await session.get(MediaRequest, current_id)
        assert current is not None and current.status == RequestStatus.searching
        rows = (await session.execute(select(Download))).scalars().all()
        assert len(rows) == 1
        assert rows[0].media_request_id == owner_id
        assert (await session.execute(select(DownloadHistory))).scalars().all() == []


async def test_grab_terminal_reuse_removes_orphan_when_parallel_active_wins(
    sessionmaker_: SessionMaker,
) -> None:
    """Terminal-row reuse must use the same IntegrityError cleanup path as create:
    if another release wins the request's active slot after qBittorrent accepted
    this torrent, remove the newly-added torrent before returning a conflict."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="Current", status=RequestStatus.searching
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=None,
                tmdb_id=100,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = _CompetingActiveDuringAddQbt(sessionmaker_, request_id, _HASH)
    async with sessionmaker_() as session:
        with pytest.raises(AlreadyDownloadingError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=request_id,
                tmdb_id=200,
            )

    assert qbt.removed == [(_HASH, True)]


async def test_grab_terminal_reuse_cas_lost_rejects_new_owner(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing a terminal row is a CAS claim. If another request moves the row back
    to active first, this grab must not return the newly active row as success."""
    async with sessionmaker_() as session:
        owner = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="Owner", status=RequestStatus.downloading
        )
        current = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="Current", status=RequestStatus.searching
        )
        session.add_all([owner, current])
        await session.flush()
        owner_id, current_id = owner.id, current.id
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=None,
                tmdb_id=999,
            )
        )
        await session.commit()

    real_update = grab_service.SqlDownloadRepository.update_status_if_in

    async def racing_update(
        self: grab_service.SqlDownloadRepository,
        download_id: int,
        status: str,
        allowed_from: frozenset[str],
        **kwargs: Any,
    ) -> bool:
        async with sessionmaker_() as other:
            row = await other.get(Download, download_id)
            assert row is not None
            row.status = "downloading"
            row.media_request_id = owner_id
            row.tmdb_id = 100
            await other.commit()
        return await real_update(self, download_id, status, allowed_from, **kwargs)

    monkeypatch.setattr(grab_service.SqlDownloadRepository, "update_status_if_in", racing_update)

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(TorrentAlreadyTrackedError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=current_id,
                tmdb_id=200,
            )

    assert qbt.added != []
    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        current = await session.get(MediaRequest, current_id)
        assert row.status == "downloading"
        assert row.media_request_id == owner_id
        assert current is not None and current.status == RequestStatus.searching
        assert (await session.execute(select(DownloadHistory))).scalars().all() == []


async def test_grab_terminal_reuse_cas_lost_to_same_request_attaches_scope(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse-race loser within ONE request: two grabs (e.g. two seasons of the same
    multi-season pack) race to resurrect the same terminal row. The loser sees the
    row now active under the SAME ``media_request_id`` and attaches its season 2
    scope to that physical row instead of raising a same-request conflict."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=900, media_type=MediaType.tv, title="Some Show", status=RequestStatus.pending
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(SeasonRequest(media_request_id=request_id, season_number=1, status="pending"))
        session.add(SeasonRequest(media_request_id=request_id, season_number=2, status="pending"))
        # The pack's prior life ended terminal (failed, not blocklisted).
        session.add(
            Download(
                torrent_hash=_HASH,
                status="failed",
                media_request_id=request_id,
                tmdb_id=900,
                failed_reason="prior failure",
            )
        )
        await session.commit()

    real_update = grab_service.SqlDownloadRepository.update_status_if_in

    async def racing_update(
        self: grab_service.SqlDownloadRepository,
        download_id: int,
        status: str,
        allowed_from: frozenset[str],
        **kwargs: Any,
    ) -> bool:
        # The SAME request's season-1 grab claims the terminal row just before this
        # (season-2) grab's CAS lands, stamping the winner's scope.
        async with sessionmaker_() as other:
            row = await other.get(Download, download_id)
            assert row is not None
            row.status = "downloading"
            row.media_request_id = request_id
            row.season = 1
            row.episodes_json = None
            await other.commit()
        return await real_update(self, download_id, status, allowed_from, **kwargs)

    monkeypatch.setattr(grab_service.SqlDownloadRepository, "update_status_if_in", racing_update)

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(_HASH, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
        )
    assert record.torrent_hash == _HASH

    async with sessionmaker_() as session:
        row = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == row.id)
                )
            )
            .scalars()
            .all()
        )
    # The winner's scalar claim is intact, and the losing season is now tracked as
    # an attached scope.
    assert row.status == "downloading"
    assert row.season == 1
    assert sorted(scope.season_number for scope in scopes if scope.season_number is not None) == [2]
    assert {s.season_number: s.status for s in seasons} == {1: "pending", 2: "downloading"}


# --------------------------------------------------------------------------- #
# Codex round-4 finding 2 (PR #117): a cancel committing while ``qbt.add`` is
# in flight must never be overwritten back to 'downloading' by grab's post-add
# status move. The move is now a CAS; a loser rolls back, removes the
# just-added torrent (the same orphan cleanup as the lost-parallel-grab
# branches), and raises the honest RequestNotActiveError. Uses a real
# file-backed engine (like the eviction double-count test): the mid-add cancel
# commits in a genuinely separate session/connection.
# --------------------------------------------------------------------------- #


class _CancelMovieDuringAddQbt(FakeQbittorrent):
    """A FakeQbittorrent whose ``add`` commits a CANCEL of the movie request in a
    separate session before returning -- the mid-add race, made deterministic."""

    def __init__(self, sm: SessionMaker, request_id: int) -> None:
        super().__init__()
        self._sm = sm
        self._request_id = request_id

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        async with self._sm() as session:
            row = await session.get(MediaRequest, self._request_id)
            assert row is not None
            row.status = RequestStatus.cancelled
            await session.commit()
        return await super().add(magnet_or_url, save_path, category)


class _CancelSeasonDuringAddQbt(FakeQbittorrent):
    """The TV twin: ``add`` commits a CANCEL of the season row mid-flight."""

    def __init__(self, sm: SessionMaker, season_request_id: int) -> None:
        super().__init__()
        self._sm = sm
        self._season_request_id = season_request_id

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        async with self._sm() as session:
            row = await session.get(SeasonRequest, self._season_request_id)
            assert row is not None
            row.status = RequestStatus.cancelled
            await session.commit()
        return await super().add(magnet_or_url, save_path, category)


async def _file_backed_sessionmaker(tmp_path: Any, name: str) -> tuple[SessionMaker, Any]:
    """A real file-backed engine (two sessions = two real connections)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from plex_manager.db import Base, enable_sqlite_fk_enforcement

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def test_grab_refuses_a_movie_cancelled_while_qbt_add_was_in_flight(
    tmp_path: Any,
) -> None:
    """The up-front terminal gate passes (row 'pending'), the cancel commits
    during the awaited qbt.add, and the post-add CAS must LOSE: no Download row,
    no 'grabbed' history, the cancel stands (never flipped back to
    'downloading'), the just-added torrent is removed with its data, and the
    caller gets the honest RequestNotActiveError."""
    sm, engine = await _file_backed_sessionmaker(tmp_path, "cancel_mid_grab_movie.db")
    try:
        async with sm() as session:
            req = MediaRequest(
                tmdb_id=700,
                media_type=MediaType.movie,
                title="Stopped",
                status=RequestStatus.pending,
            )
            session.add(req)
            await session.commit()
            request_id = req.id

        qbt = _CancelMovieDuringAddQbt(sm, request_id)
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt, session, scored=_scored(_HASH), request_id=request_id, tmdb_id=700
                )

        # The just-added torrent was cleaned up WITH its data (orphan cleanup).
        assert (_HASH, True) in qbt.removed
        async with sm() as session:
            row = await session.get(MediaRequest, request_id)
            downloads = (
                (await session.execute(select(Download).where(Download.torrent_hash == _HASH)))
                .scalars()
                .all()
            )
            history = (
                (
                    await session.execute(
                        select(DownloadHistory).where(DownloadHistory.tmdb_id == 700)
                    )
                )
                .scalars()
                .all()
            )
        assert row is not None
        assert row.status is RequestStatus.cancelled  # the user's stop STANDS
        assert downloads == []  # no tracked download for cancelled content
        assert history == []  # no 'grabbed' record for a refused grab
    finally:
        await engine.dispose()


async def test_grab_refuses_a_season_cancelled_while_qbt_add_was_in_flight(
    tmp_path: Any,
) -> None:
    """The TV twin: the season row is cancelled mid-add. The post-add season CAS
    loses (cancelled is the ONE excluded season status -- the deliberate
    reopen of available/completed seasons still works), the parent rollup is
    never recomputed off a write that lost, and the torrent is cleaned up."""
    sm, engine = await _file_backed_sessionmaker(tmp_path, "cancel_mid_grab_tv.db")
    try:
        async with sm() as session:
            show = MediaRequest(
                tmdb_id=701,
                media_type=MediaType.tv,
                title="Stopped Show",
                status=RequestStatus.pending,
            )
            session.add(show)
            await session.flush()
            season_row = SeasonRequest(
                media_request_id=show.id, season_number=1, status=RequestStatus.pending
            )
            session.add(season_row)
            await session.commit()
            show_id, season_id = show.id, season_row.id

        qbt = _CancelSeasonDuringAddQbt(sm, season_id)
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored(_HASH),
                    request_id=show_id,
                    tmdb_id=701,
                    season=1,
                )

        assert (_HASH, True) in qbt.removed
        async with sm() as session:
            season_row = await session.get(SeasonRequest, season_id)
            show = await session.get(MediaRequest, show_id)
            downloads = (
                (await session.execute(select(Download).where(Download.torrent_hash == _HASH)))
                .scalars()
                .all()
            )
        assert season_row is not None
        assert season_row.status is RequestStatus.cancelled  # never un-cancelled
        assert show is not None
        assert show.status is RequestStatus.pending  # rollup untouched by the loser
        assert downloads == []
    finally:
        await engine.dispose()


async def test_grab_still_reopens_an_available_season_after_the_cas(
    sessionmaker_: SessionMaker,
) -> None:
    """Regression guard for the season CAS scope: grab must STILL be able to
    reopen an already-'available' season (chasing one more missing episode).
    With the round-5 exact-observed-status CAS this works because the DECISION
    itself observed 'available' -- so the post-add move compares 'available'
    and wins; only a status that CHANGED underneath the in-flight add (a
    cancel, or the recovery fold) loses."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=702,
            media_type=MediaType.tv,
            title="Reopen Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.available
        )
        session.add(season_row)
        await session.commit()
        show_id, season_id = show.id, season_row.id

    # NOTE: the parent is terminal ('available'), which the up-front gate would
    # refuse -- reopening goes through an ACTIVE parent in practice. Re-arm the
    # parent as the re-request path would (partially_available is active).
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.partially_available
        await session.commit()

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=show_id,
            tmdb_id=702,
            season=1,
        )
    assert record.status == "downloading"
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.downloading  # the reopen still works


# --------------------------------------------------------------------------- #
# Codex round-5 finding 1 (PR #117): the post-add CAS compares EXACTLY the
# status the grab decision observed. The eviction recovery's failed-purge fold
# (pending -> available: the file never left disk) landing while qbt.add is in
# flight must make the grab lose -- otherwise it commits a duplicate download
# for on-disk content -- while the INTENTIONAL reopen (the decision itself
# observed 'available') keeps working because 'available' is what it compares.
# --------------------------------------------------------------------------- #


class _FoldSeasonDuringAddQbt(FakeQbittorrent):
    """A FakeQbittorrent whose ``add`` commits the eviction recovery's FOLD
    (``pending`` -> ``available``) of the season row in a separate session
    before returning -- the fold-vs-grab race, made deterministic."""

    def __init__(
        self,
        sm: SessionMaker,
        season_request_id: int,
        status: RequestStatus = RequestStatus.available,
    ) -> None:
        super().__init__()
        self._sm = sm
        self._season_request_id = season_request_id
        self._status = status

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        async with self._sm() as session:
            row = await session.get(SeasonRequest, self._season_request_id)
            assert row is not None
            row.status = self._status
            await session.commit()
        return await super().add(magnet_or_url, save_path, category)


async def test_grab_loses_to_a_recovery_fold_that_landed_while_qbt_add_was_in_flight(
    tmp_path: Any,
) -> None:
    """The grab decision observed the season as 'pending'; the eviction
    recovery folded it to 'available' (its file never left disk) during the
    awaited qbt.add. The post-add CAS compares the OBSERVED status ('pending'),
    so it must lose: no duplicate download for on-disk content, the fold
    stands, the just-added torrent is removed, honest RequestNotActiveError."""
    sm, engine = await _file_backed_sessionmaker(tmp_path, "fold_mid_grab_tv.db")
    try:
        async with sm() as session:
            show = MediaRequest(
                tmdb_id=710,
                media_type=MediaType.tv,
                title="Folded Show",
                status=RequestStatus.pending,
            )
            session.add(show)
            await session.flush()
            season_row = SeasonRequest(
                media_request_id=show.id, season_number=1, status=RequestStatus.pending
            )
            session.add(season_row)
            await session.commit()
            show_id, season_id = show.id, season_row.id

        qbt = _FoldSeasonDuringAddQbt(sm, season_id)
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored(_HASH),
                    request_id=show_id,
                    tmdb_id=710,
                    season=1,
                )

        assert (_HASH, True) in qbt.removed  # the just-added torrent was cleaned up
        async with sm() as session:
            season_row = await session.get(SeasonRequest, season_id)
            downloads = (
                (await session.execute(select(Download).where(Download.torrent_hash == _HASH)))
                .scalars()
                .all()
            )
            history = (
                (
                    await session.execute(
                        select(DownloadHistory).where(DownloadHistory.tmdb_id == 710)
                    )
                )
                .scalars()
                .all()
            )
        assert season_row is not None
        assert season_row.status is RequestStatus.available  # the fold STANDS
        assert downloads == []  # no duplicate download for on-disk content
        assert history == []  # no 'grabbed' record for a refused grab
    finally:
        await engine.dispose()


@pytest.mark.parametrize("settled_status", [RequestStatus.available, RequestStatus.completed])
async def test_pack_grab_does_not_reopen_a_sibling_settled_during_add(
    tmp_path: Any,
    settled_status: RequestStatus,
) -> None:
    """A pack owns only siblings that remain searchable through its post-add CAS.

    The planner selected S1+S2 while both were pending, but S2 settled while the
    physical torrent was being added. The pack must lose atomically instead of
    moving S2 back to downloading and re-downloading content already on disk.
    """
    sm, engine = await _file_backed_sessionmaker(
        tmp_path, f"pack_sibling_{settled_status.value}_mid_grab.db"
    )
    try:
        request_id = await _make_tv_request(sm, tmdb_id=712)
        async with sm() as session:
            season_2_id = await session.scalar(
                select(SeasonRequest.id).where(
                    SeasonRequest.media_request_id == request_id,
                    SeasonRequest.season_number == 2,
                )
            )
        assert season_2_id is not None

        pack = _scored_tv(_HASH, "Some.Show.S01-S02.COMPLETE.1080p.WEB-DL.x264-GROUP").model_copy(
            update={"target_seasons": (1, 2)}
        )
        qbt = _FoldSeasonDuringAddQbt(sm, season_2_id, settled_status)
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt,
                    session,
                    scored=pack,
                    request_id=request_id,
                    tmdb_id=712,
                    season=1,
                )

        assert qbt.removed == [(_HASH, True)]
        async with sm() as session:
            seasons = (
                (
                    await session.execute(
                        select(SeasonRequest)
                        .where(SeasonRequest.media_request_id == request_id)
                        .order_by(SeasonRequest.season_number)
                    )
                )
                .scalars()
                .all()
            )
            downloads = (await session.execute(select(Download))).scalars().all()
            scopes = (await session.execute(select(DownloadScope))).scalars().all()
            history = (await session.execute(select(DownloadHistory))).scalars().all()
        assert [(row.season_number, row.status) for row in seasons] == [
            (1, RequestStatus.pending),
            (2, settled_status),
        ]
        assert downloads == []
        assert scopes == []
        assert history == []
    finally:
        await engine.dispose()


async def test_grab_refuses_an_already_cancelled_season_before_adding_anything(
    sessionmaker_: SessionMaker,
) -> None:
    """A season observed as 'cancelled' at DECISION time is refused up front,
    like a terminal request -- nothing is ever handed to the client (no add, no
    cleanup churn), the season-level mirror of the request terminal gate."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=711,
            media_type=MediaType.tv,
            title="Stopped Season Show",
            status=RequestStatus.pending,  # parent active (another season in flight)
        )
        session.add(show)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.cancelled
        )
        session.add(season_row)
        await session.commit()
        show_id = show.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(RequestNotActiveError):
            await grab_service.grab(
                qbt, session, scored=_scored(_HASH), request_id=show_id, tmdb_id=711, season=1
            )
    assert qbt.added == []  # refused BEFORE anything reached the client


# --------------------------------------------------------------------------- #
# Codex round-8 finding 2: the lost-grab cleanup removes ONLY torrents this
# grab genuinely created. qbt.add's 409-already-present branch resolves to a
# PRE-EXISTING torrent (AddResult.created False) -- e.g. a still-seeding import
# whose data may back a live library file via hardlink -- and the DB rollback
# preserved whatever row tracked it, so removing it with delete_files would
# destroy content the grab never owned.
# --------------------------------------------------------------------------- #


async def test_grab_loss_cleanup_leaves_a_pre_existing_torrent_untouched(
    tmp_path: Any,
) -> None:
    """Terminal-row reuse of a torrent the client reported ALREADY PRESENT
    (created=False), then the post-add CAS loses to a mid-add cancel: the
    cleanup must NOT remove the pre-existing torrent (it predates this grab;
    the rollback preserved the old terminal row that tracks it)."""
    sm, engine = await _file_backed_sessionmaker(tmp_path, "preexisting_cleanup.db")
    try:
        async with sm() as session:
            req = MediaRequest(
                tmdb_id=720,
                media_type=MediaType.movie,
                title="Preexisting",
                status=RequestStatus.pending,
            )
            session.add(req)
            await session.flush()
            request_id = req.id
            # The old terminal row that tracks the still-present torrent.
            session.add(
                Download(
                    torrent_hash=_HASH,
                    status="failed",
                    media_request_id=request_id,
                    tmdb_id=720,
                    failed_reason="prior failure",
                )
            )
            await session.commit()

        qbt = _CancelMovieDuringAddQbt(sm, request_id)
        qbt.pre_existing = {_HASH}  # add() reports 409-already-present
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt, session, scored=_scored(_HASH), request_id=request_id, tmdb_id=720
                )

        # The pre-existing torrent was left completely untouched.
        assert qbt.removed == []
        async with sm() as session:
            row = (
                await session.execute(select(Download).where(Download.torrent_hash == _HASH))
            ).scalar_one()
            request_row = await session.get(MediaRequest, request_id)
        assert row.status == "failed"  # the rollback preserved the old terminal row
        assert request_row is not None
        assert request_row.status is RequestStatus.cancelled  # the cancel stands
    finally:
        await engine.dispose()


async def test_grab_loss_cleanup_still_removes_a_genuinely_added_torrent(
    tmp_path: Any,
) -> None:
    """The counterpart: a torrent this grab genuinely created (created=True)
    IS removed with its data on a lost post-add CAS -- unchanged behavior."""
    sm, engine = await _file_backed_sessionmaker(tmp_path, "genuine_cleanup.db")
    try:
        async with sm() as session:
            req = MediaRequest(
                tmdb_id=721,
                media_type=MediaType.movie,
                title="Fresh Add",
                status=RequestStatus.pending,
            )
            session.add(req)
            await session.commit()
            request_id = req.id

        qbt = _CancelMovieDuringAddQbt(sm, request_id)  # created=True by default
        async with sm() as session:
            with pytest.raises(RequestNotActiveError):
                await grab_service.grab(
                    qbt, session, scored=_scored(_HASH), request_id=request_id, tmdb_id=721
                )

        assert (_HASH, True) in qbt.removed  # the orphan this grab created is cleaned up
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Codex round-9 (PR #117): the caller's premise rides with the action.
# Auto-grab selects a scope because the season read as DUE; if the eviction
# recovery folds it to 'available' before grab()'s own fresh read, the fresh
# observation would mistake the fold for an intentional reopen (the round-5
# observed-status CAS cannot help: the observation post-dates the fold).
# expected_season_status lets grab refuse up front, before anything reaches
# the client; the manual reopen flow states no premise and keeps working.
# --------------------------------------------------------------------------- #


async def test_grab_refuses_when_the_callers_premise_no_longer_holds(
    sessionmaker_: SessionMaker,
) -> None:
    """Auto-grab's premise ('this season is due: pending') no longer holds --
    the recovery fold landed first and the season is 'available'. The grab is
    refused BEFORE anything reaches the client: no add, no cleanup churn, no
    duplicate download of on-disk content."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=730,
            media_type=MediaType.tv,
            title="Folded Before Grab",
            status=RequestStatus.pending,
        )
        session.add(show)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=show.id,
            season_number=1,
            status=RequestStatus.available,  # the recovery fold landed first
        )
        session.add(season_row)
        await session.commit()
        show_id, season_id = show.id, season_row.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(RequestNotActiveError):
            await grab_service.grab(
                qbt,
                session,
                scored=_scored(_HASH),
                request_id=show_id,
                tmdb_id=730,
                season=1,
                expected_season_status=RequestStatus.pending.value,  # the due premise
            )

    assert qbt.added == []  # refused up front -- nothing reached the client
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.available  # the fold stands


async def test_grab_proceeds_when_the_callers_premise_holds(
    sessionmaker_: SessionMaker,
) -> None:
    """The premise check is a no-op when the premise still holds: a due season
    grabbed with its selection-time status proceeds exactly as before."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=731,
            media_type=MediaType.tv,
            title="Still Due",
            status=RequestStatus.pending,
        )
        session.add(show)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.pending
        )
        session.add(season_row)
        await session.commit()
        show_id, season_id = show.id, season_row.id

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_HASH),
            request_id=show_id,
            tmdb_id=731,
            season=1,
            expected_season_status=RequestStatus.pending.value,
        )
    assert record.status == "downloading"
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.downloading


# --------------------------------------------------------------------------- #
# Issue #103: same-release re-grab must be idempotent when the indexer omits
# ``info_hash`` -- the pre-add parallel-grab guard is gated on
# ``known_hash is not None`` so a hashless candidate cannot 409 a legitimate
# same-release UI retry before ``qbt.add`` resolves the real hash.
# --------------------------------------------------------------------------- #


def _scored_hashless(
    magnet_url: str, title: str = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
) -> ScoredRelease:
    """A :class:`ScoredRelease` whose candidate carries NO ``info_hash`` (the
    indexer omitted it) but resolves to a deterministic hash via ``magnet_url``
    once ``qbt.add`` parses it -- mirrors a real hashless Prowlarr result."""
    cand = candidate(title, info_hash=None).model_copy(update={"magnet_url": magnet_url})
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Movie", source=QualitySource.WEBDL
    )
    return ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )


async def test_grab_hashless_same_release_retry_is_idempotent_not_409(
    sessionmaker_: SessionMaker,
) -> None:
    """A hashless candidate for the SAME release, re-grabbed for the SAME
    request (a UI double-click retry), must NOT 409 -- it should resolve to the
    existing active row once ``qbt.add`` reports the real (matching) hash, not
    raise ``AlreadyDownloadingError`` before the client is even asked."""
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.searching
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        await session.commit()

    magnet = "magnet:?xt=urn:btih:" + "c" * 40
    first_qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            first_qbt,
            session,
            scored=_scored_hashless(magnet),
            request_id=req_id,
            tmdb_id=100,
        )
    assert first.status == "downloading"
    assert first.torrent_hash == "c" * 40

    # The retry: qBittorrent now reports the SAME hash as already present
    # (created=False), exactly the real client's 409-on-duplicate-add behavior.
    retry_qbt = FakeQbittorrent(pre_existing={"c" * 40})
    async with sessionmaker_() as session:
        again = await grab_service.grab(
            retry_qbt,
            session,
            scored=_scored_hashless(magnet),
            request_id=req_id,
            tmdb_id=100,
        )
    assert again.id == first.id  # same row returned -- no spurious 409

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.media_request_id == req_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # no second row was ever created


async def test_grab_hashless_different_release_still_rejected(
    sessionmaker_: SessionMaker,
) -> None:
    """A hashless candidate for a GENUINELY DIFFERENT release than the one
    already active for this request is still refused honestly -- deferring the
    pre-add guard does not let a second concurrent release through. Caught
    post-add via the ``uq_downloads_active_request`` backstop, which removes
    the torrent this call just added before raising ``AlreadyDownloadingError``.
    """
    async with sessionmaker_() as session:
        req = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.searching
        )
        session.add(req)
        await session.flush()
        req_id = req.id
        await session.commit()

    first_magnet = "magnet:?xt=urn:btih:" + "d" * 40
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_hashless(first_magnet, "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"),
            request_id=req_id,
            tmdb_id=100,
        )
    assert first.status == "downloading"

    second_magnet = "magnet:?xt=urn:btih:" + "e" * 40
    second_qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(AlreadyDownloadingError):
            await grab_service.grab(
                second_qbt,
                session,
                scored=_scored_hashless(second_magnet, "Some.Movie.2020.2160p.WEB-DL.x264-GROUP"),
                request_id=req_id,
                tmdb_id=100,
            )

    # The just-added (losing) torrent was cleaned up rather than left orphaned.
    assert second_qbt.removed == [("e" * 40, True)]

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(Download).where(Download.media_request_id == req_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].torrent_hash == "d" * 40  # only the original release is tracked


# --------------------------------------------------------------------------- #
# Issue #102: ``episodes=[]`` and ``episodes=None`` both mean "whole season" --
# the same-hash scope attachment path (exercised here through the public
# ``grab()`` API) must treat the two identically in BOTH directions, whether the
# empty list originates from the ACTIVE row or the REQUESTED scope (e.g. a row
# persisted by a caller that bypasses the pydantic schema layer's ``[]`` ->
# ``None`` normalization -- as calling ``grab_service.grab`` directly, below,
# does).
# --------------------------------------------------------------------------- #


async def test_reuse_same_hash_active_episodes_empty_vs_requested_none_non_conflicting(
    sessionmaker_: SessionMaker,
) -> None:
    """Direction 1: the ACTIVE row persisted ``episodes=[]`` (a caller that
    bypassed the schema-layer normalization) and the SAME-hash re-grab requests
    whole-season (``episodes=None``) -- must be an idempotent no-op, not a scope
    conflict."""
    request_id = await _make_tv_request(sessionmaker_)
    h = "9" * 40
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[],
        )
    assert first.episodes == []

    async with sessionmaker_() as session:
        again = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=None,
        )
    assert again.id == first.id


async def test_reuse_same_hash_active_episodes_none_vs_requested_empty_non_conflicting(
    sessionmaker_: SessionMaker,
) -> None:
    """Direction 2 (the reverse): the ACTIVE row is whole-season (``None``) and
    the SAME-hash re-grab carries an unnormalized ``episodes=[]`` -- must also
    be a non-conflicting no-op."""
    request_id = await _make_tv_request(sessionmaker_)
    h = "a" * 40
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=None,
        )
    assert first.episodes is None

    async with sessionmaker_() as session:
        again = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[],
        )
    assert again.id == first.id


async def test_reuse_same_hash_active_episodes_empty_both_sides_non_conflicting(
    sessionmaker_: SessionMaker,
) -> None:
    """Both the active row and the re-grab request carry an unnormalized
    ``[]`` -- still non-conflicting (whole-season covers whole-season)."""
    request_id = await _make_tv_request(sessionmaker_)
    h = "b" * 40
    async with sessionmaker_() as session:
        first = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[],
        )
    assert first.episodes == []

    async with sessionmaker_() as session:
        again = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored_tv(h, "Some.Show.S02.1080p.WEB-DL.x264-GROUP"),
            request_id=request_id,
            tmdb_id=900,
            season=2,
            episodes=[],
        )
    assert again.id == first.id


async def test_grab_stamps_metadata_timeout(sessionmaker_: SessionMaker) -> None:
    """A fresh grab stamps ``timeout_at`` = ``added_at`` + the metadata-fetch
    window (observability only -- detect_stalls stays anchored on added_at)."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=300, media_type=MediaType.movie, title="C", status=RequestStatus.searching
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()

    h = "c" * 40
    before = datetime.now(UTC)
    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(h),
            request_id=request_id,
            tmdb_id=300,
        )
    after = datetime.now(UTC)

    async with sessionmaker_() as session:
        row = await session.get(Download, record.id)
        assert row is not None
        assert row.added_at is not None
        assert row.timeout_at is not None
        # SQLite round-trips a ``DateTime(timezone=True)`` value as naive (UTC
        # wall-clock, tzinfo dropped) -- reattach UTC before comparing against
        # the tz-aware wall-clock window captured around the call. ``added_at``
        # is a DB server default (second resolution) while ``timeout_at`` is
        # stamped in Python (microsecond) -- the tiny clock skew between the two
        # is irrelevant for an observability column (blueprint note).
        timeout_at = row.timeout_at.replace(tzinfo=UTC)
        assert before + METADATA_STALL_WINDOW <= timeout_at <= after + METADATA_STALL_WINDOW


# --------------------------------------------------------------------------- #
# #206: the terminal-row reuse path must refuse to resurrect a row whose
# torrent removal is currently in flight (a concurrent cancel's post-commit
# delete, or a reconcile-driven removal) -- reusing it would hand the new
# request a torrent whose data is mid-deletion.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clear_removals_in_flight() -> None:
    """Isolate the module-global removal-in-flight registry between tests: a
    leaked id (from a failing test) must never refuse an unrelated grab."""
    queue_service._removals_in_flight.clear()  # pyright: ignore[reportPrivateUsage]


async def _seed_terminal_download(sm: SessionMaker, *, torrent_hash: str) -> tuple[int, int, int]:
    """Old request A owns a terminal (failed) download for ``torrent_hash``; new
    request B is freshly created for the same title. Returns (old_id, new_id,
    download_id)."""
    async with sm() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.cancelled
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="B", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        download = Download(
            torrent_hash=torrent_hash,
            status="failed",
            media_request_id=old_id,
            tmdb_id=100,
            failed_reason="cancelled by operator",
        )
        session.add(download)
        await session.commit()
        download_id = download.id
    return old_id, new_id, download_id


async def test_reuse_refused_while_removal_in_flight(sessionmaker_: SessionMaker) -> None:
    """The KNOWN-HASH pre-add guard (grab_service.py, the known-hash precheck):
    when the indexer supplied the hash and it maps to a terminal row whose removal
    is in flight, the grab is refused BEFORE ``qbt.add`` ever runs -- nothing is
    added (so no fresh torrent is orphaned to seed untracked) and the terminal row
    stays exactly as it was, still owned by the OLD request, never repointed."""
    h = "1" * 40
    old_id, new_id, download_id = await _seed_terminal_download(sessionmaker_, torrent_hash=h)

    qbt = FakeQbittorrent()
    queue_service.register_removal_in_flight(download_id)
    try:
        async with sessionmaker_() as session:
            with pytest.raises(TorrentRemovalInFlightError) as excinfo:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored(h),
                    request_id=new_id,
                    tmdb_id=200,
                )
        assert excinfo.value.torrent_hash == h
        assert excinfo.value.download_id == download_id
    finally:
        queue_service.release_removal_in_flight(download_id)

    # Refused up front: the known hash let us gate BEFORE the client, so nothing was
    # ever handed to qBittorrent (no orphan torrent) and nothing removed.
    assert qbt.added == []
    assert qbt.removed == []

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "failed"  # still terminal, not resurrected
    assert row.media_request_id == old_id  # NOT repointed to the new request


async def test_reuse_refused_after_add_cleans_up_the_orphan_torrent(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1 (#206): a HASHLESS candidate cannot be gated before the add -- the
    real hash is only known once ``qbt.add`` returns -- so the reuse guard fires in
    ``_reuse_terminal_row`` AFTER the add. When this grab genuinely CREATED the
    torrent (``created=True``, qBittorrent had dropped the hash mid-removal), refusing
    without cleanup would leave it seeding untracked. The guard must remove that fresh
    torrent (WITH data) before raising, and leave the terminal row untouched."""
    h = "7" * 40
    old_id, new_id, download_id = await _seed_terminal_download(sessionmaker_, torrent_hash=h)

    qbt = FakeQbittorrent()  # created=True for the freshly added hash
    queue_service.register_removal_in_flight(download_id)
    try:
        async with sessionmaker_() as session:
            with pytest.raises(TorrentRemovalInFlightError) as excinfo:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored_hashless("magnet:?xt=urn:btih:" + h),
                    request_id=new_id,
                    tmdb_id=200,
                )
        assert excinfo.value.download_id == download_id
    finally:
        queue_service.release_removal_in_flight(download_id)

    # The add DID happen (hashless -> the guard can only fire post-add), and the
    # orphaned fresh torrent was removed WITH its data before raising.
    assert len(qbt.added) == 1
    assert (h, True) in qbt.removed

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "failed"  # still terminal, not resurrected
    assert row.media_request_id == old_id  # NOT repointed to the new request


async def test_reuse_refused_after_add_leaves_a_pre_existing_torrent_in_place(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1 counterpart: when the post-add guard fires but the torrent was
    already PRESENT (``created=False`` -- it predates this grab, e.g. a still-seeding
    import whose data may back a live library file), it is NOT ours to destroy. The
    guard refuses without removing anything -- the pre-existing torrent is left
    untouched, mirroring ``_remove_torrent_if_added``'s gate."""
    h = "8" * 40
    old_id, new_id, download_id = await _seed_terminal_download(sessionmaker_, torrent_hash=h)

    qbt = FakeQbittorrent(pre_existing={h})  # created=False for this hash
    queue_service.register_removal_in_flight(download_id)
    try:
        async with sessionmaker_() as session:
            with pytest.raises(TorrentRemovalInFlightError):
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored_hashless("magnet:?xt=urn:btih:" + h),
                    request_id=new_id,
                    tmdb_id=200,
                )
    finally:
        queue_service.release_removal_in_flight(download_id)

    assert len(qbt.added) == 1  # the add was attempted (hashless)
    assert qbt.removed == []  # but the pre-existing torrent was left untouched

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "failed"
    assert row.media_request_id == old_id


async def test_reuse_refused_while_operator_removal_in_flight(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 3 (#206, operator-delete arm): an operator ``mark_failed(remove_torrent
    =True)`` records its irreversible torrent delete ONLY on its ``_OperatorClaim``
    (the ``removal_in_flight`` flag), NEVER in the shared ``_removals_in_flight``
    registry. The reuse guard's single query point must still see it, so a same-hash
    grab racing that operator delete -- after a cancel committed the row terminal and
    released ITS OWN shared guard -- is refused rather than reusing a row whose data
    the operator is mid-deletion. Would silently re-own the row before the fix."""
    h = "9" * 40
    old_id, new_id, download_id = await _seed_terminal_download(sessionmaker_, torrent_hash=h)

    flags = queue_service._OperatorFailFlags(  # pyright: ignore[reportPrivateUsage]
        blocklist=True, remove_torrent=True
    )
    token = queue_service._register_operator_claim(download_id, flags)  # pyright: ignore[reportPrivateUsage]
    queue_service._mark_removal_in_flight(download_id, token)  # pyright: ignore[reportPrivateUsage]
    qbt = FakeQbittorrent()
    try:
        # Nothing was ever published to the SHARED registry -- the operator flag is
        # the only record of this in-flight delete.
        assert download_id not in queue_service._removals_in_flight  # pyright: ignore[reportPrivateUsage]
        async with sessionmaker_() as session:
            with pytest.raises(TorrentRemovalInFlightError) as excinfo:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=_scored(h),
                    request_id=new_id,
                    tmdb_id=200,
                )
        assert excinfo.value.download_id == download_id
        assert qbt.added == []  # known-hash pre-add guard refused before the client
    finally:
        queue_service._release_operator_claim(download_id, token)  # pyright: ignore[reportPrivateUsage]

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "failed"  # still terminal, not resurrected
    assert row.media_request_id == old_id  # NOT repointed to the new request
    assert not queue_service.removal_in_flight(download_id)  # released with the claim


async def test_reuse_succeeds_after_removal_released(sessionmaker_: SessionMaker) -> None:
    """No regression to the ordinary reuse path: with nothing registered (or a
    claim registered then released before the grab), the terminal row is reused
    and re-owned to the new request exactly as before #206."""
    h = "2" * 40
    _old_id, new_id, download_id = await _seed_terminal_download(sessionmaker_, torrent_hash=h)

    # Register then release, proving a settled (no longer in-flight) claim does
    # not linger and block a later, legitimate reuse.
    queue_service.register_removal_in_flight(download_id)
    queue_service.release_removal_in_flight(download_id)

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(h),
            request_id=new_id,
            tmdb_id=200,
        )
    assert record.status == "downloading"

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "downloading"
    assert row.media_request_id == new_id  # re-owned to the CURRENT request
    assert not queue_service.removal_in_flight(download_id)


async def test_reuse_refused_while_removal_in_flight_via_unique_conflict(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME guard also covers the second reuse call site (grab_service.py
    ~887, reached from the create-time ``IntegrityError`` branch): a genuine
    DB-level race -- a competing terminal (cancelled) row for the SAME hash
    lands between this call's ``get_by_hash`` miss and its own ``create()`` --
    is recovered by re-reading the winner and calling the SAME shared
    ``_reuse_terminal_row`` helper. Proves the guard is not accidentally
    call-site-specific."""
    h = "3" * 40
    async with sessionmaker_() as session:
        old = MediaRequest(
            tmdb_id=100, media_type=MediaType.movie, title="A", status=RequestStatus.cancelled
        )
        new = MediaRequest(
            tmdb_id=200, media_type=MediaType.movie, title="B", status=RequestStatus.searching
        )
        session.add_all([old, new])
        await session.flush()
        old_id, new_id = old.id, new.id
        await session.commit()

    registered_ids: list[int] = []
    real_create = grab_service.SqlDownloadRepository.create

    async def racing_create(
        self: grab_service.SqlDownloadRepository, **kwargs: Any
    ) -> DownloadRecord:
        # Mirrors the existing racing-update fakes above: a concurrent actor
        # (here, a cancel that already committed its terminal move) inserts the
        # SAME hash as a terminal row just before this call's own INSERT lands
        # -- the genuine DB-level race the ``except IntegrityError`` recovery
        # (grab_service.py ~830) exists for. Registering the removal claim here,
        # before the real ``create()`` raises, mirrors cancel's own
        # register-before-commit ordering (#206): by the time this call's
        # IntegrityError recovery reaches ``_reuse_terminal_row``, the claim is
        # already in place.
        async with sessionmaker_() as other:
            competing = Download(
                torrent_hash=h,
                status="failed",
                media_request_id=old_id,
                tmdb_id=100,
                failed_reason="cancelled by operator",
            )
            other.add(competing)
            await other.commit()
            registered_ids.append(competing.id)
        queue_service.register_removal_in_flight(competing.id)
        return await real_create(self, **kwargs)

    monkeypatch.setattr(grab_service.SqlDownloadRepository, "create", racing_create)

    try:
        async with sessionmaker_() as session:
            with pytest.raises(TorrentRemovalInFlightError):
                await grab_service.grab(
                    FakeQbittorrent(),
                    session,
                    scored=_scored_hashless("magnet:?xt=urn:btih:" + h),
                    request_id=new_id,
                    tmdb_id=200,
                )
    finally:
        for download_id in registered_ids:
            queue_service.release_removal_in_flight(download_id)

    assert len(registered_ids) == 1
    async with sessionmaker_() as session:
        row = await session.get(Download, registered_ids[0])
    assert row is not None
    assert row.status == "failed"  # still terminal, not resurrected
    assert row.media_request_id == old_id  # NOT repointed to the new request
