"""grab_service — terminal-row reuse re-owns to the current request (defensive)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.release import ParsedRelease, ScoredRelease
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.services import grab_service
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    GrabError,
    RequestNotActiveError,
)
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
    assert row.failed_reason is None  # stale failure reason cleared


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
