"""Correction verbs (ADR-0014): report-issue and cancel.

Exercises the full report-issue flow (blocklist -> remove torrent -> purge file ->
scan -> re-arm -> audit -> inline re-grab) and the cancel flow against the REAL
``LocalFileSystem`` (root guard genuinely exercised), the REAL ``GuessitParser`` +
default quality profile (the decision engine is genuinely run), and fakes only at
the I/O edges (qBittorrent / Prowlarr / Plex).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.services import correction_service
from tests.web.fakes import FakeLibrary, FakeProwlarr, FakeQbittorrent, candidate

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB = 603
_CULPRIT = "3" * 40
_ALT = "a" * 40


async def _seed_available_movie(
    sm: SessionMaker, *, library_path: str, culprit_hash: str = _CULPRIT
) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=library_path,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=culprit_hash,
                status="imported",
                media_request_id=request.id,
                tmdb_id=_TMDB,
                year=2020,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=_TMDB,
                torrent_hash=culprit_hash,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Movie.2020.1080p.BluRay.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        return request.id


async def test_report_issue_movie_blocklists_purges_removes_and_regrabs(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    fs = LocalFileSystem(library_roots=[str(root)])
    library = FakeLibrary()
    # The culprit (BluRay) would OUTRANK the alternative (WEB-DL) if research ran
    # before the blocklist -- so grabbing the alternative proves blocklist-then-
    # research ordering, not merely "some release was grabbed".
    prowlarr = FakeProwlarr(
        [
            candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT),
        ]
    )

    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            fs,
            library,
            prowlarr,
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            root_path=str(root),
        )

    # (c) the library file was purged, (b) the culprit torrent removed WITH data.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    # (d) a Plex scan fired for the removal.
    assert library.scan_calls == [(str(movie_file), "movie")]
    # (g) the inline re-grab landed on 'downloading' -- a replacement is in flight.
    assert updated.status == RequestStatus.downloading.value

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.reported
                    )
                )
            )
            .scalars()
            .all()
        )

    # (a) the culprit release is blocklisted (movie-scoped).
    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _CULPRIT
    assert blocklist[0].reason == "bad_quality"
    assert blocklist[0].media_type == "movie"
    # blocklist-THEN-research: the re-grab picked the ALTERNATIVE, never the
    # (now-blocklisted) culprit, even though the culprit would rank higher.
    active_hashes = {d.torrent_hash for d in downloads if d.status != "imported"}
    assert active_hashes == {_ALT}
    # (f) an audit row was written.
    assert len(history) == 1


async def test_report_issue_movie_reset_clears_library_path(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 1024)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            FakeQbittorrent(),
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            # Nothing acceptable -> parks; the reset/clear still must have happened.
            FakeProwlarr([]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="user_reported",
            season=None,
            root_path=str(root),
        )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    # Nothing acceptable after the blocklist -> honest, retryable park; the stale
    # library_path/completed anchors were cleared (the file is gone).
    assert request.status == RequestStatus.no_acceptable_release.value
    assert request.library_path is None
    assert request.completed_at is None


async def test_report_issue_refuses_when_media_root_unmounted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Foot-gun failsafe: an unmounted/empty root aborts the WHOLE verb -- nothing
    # is blocklisted, removed, purged, or flipped (the file isn't really gone).
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 512)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    missing_root = tmp_path / "unmounted"  # never created -> not a mount

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                root_path=str(missing_root),
            )

    assert movie_file.exists()  # nothing was purged
    assert qbt.removed == []  # nothing was removed
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status == RequestStatus.available.value  # untouched
    assert blocklist == []


async def test_report_issue_rejects_a_not_reportable_movie(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    (root / "keep").write_bytes(b"x")  # non-empty so the failsafe wouldn't fire
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.searching,  # not imported/available -> not reportable
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    with pytest.raises(correction_service.NotReportableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                FakeQbittorrent(),
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                root_path=str(root),
            )


async def test_report_issue_tv_season_purges_and_parks_on_nothing_acceptable(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Some Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id,
                season_number=1,
                status=RequestStatus.available,
                library_path=str(season_dir),
            )
        )
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=1399,
                torrent_hash=_CULPRIT,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Show.S01.1080p.WEB-DL.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            # Only the culprit is on offer -> blocklisted -> nothing acceptable ->
            # the season parks honestly (also proves blocklist-then-research).
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            root_path=str(tv_root),
        )

    assert not season_dir.exists()  # the season tree was purged
    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        season = (
            await session.execute(
                select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
            )
        ).scalar_one()
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    # The season re-armed, found nothing acceptable (only the blocklisted culprit),
    # and parked honestly; its purge breadcrumb was cleared.
    assert season.status == RequestStatus.no_acceptable_release
    assert season.library_path is None
    assert len(blocklist) == 1
    assert blocklist[0].media_type == "tv"


async def test_report_issue_tv_requires_a_season(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "tv"
    root.mkdir()
    (root / "keep").write_bytes(b"x")
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=1, status=RequestStatus.available)
        )
        await session.commit()
        request_id = show.id

    with pytest.raises(correction_service.ReportSeasonRequiredError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                FakeQbittorrent(),
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                root_path=str(root),
            )


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #
async def test_cancel_movie_removes_torrent_and_settles_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT))
        ).scalar_one()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.cancelled
                    )
                )
            )
            .scalars()
            .all()
        )
    assert download.status == "failed"
    assert download.failed_reason == "cancelled by operator"
    assert len(history) == 1


async def test_cancel_already_gone_torrent_is_a_no_op_success(
    sessionmaker_: SessionMaker,
) -> None:
    # The download's torrent isn't in the client (already gone): remove is a no-op
    # success and the cancel still settles cleanly.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    # FakeQbittorrent with NO statuses -> the hash is "already gone"; remove() still
    # succeeds (mirrors qBittorrent's /torrents/delete tolerating an unknown hash).
    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)
    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed


async def test_cancel_tv_settles_every_season_and_rolls_up_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id, season_number=1, status=RequestStatus.downloading
            )
        )
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=2, status=RequestStatus.searching)
        )
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
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
    assert {s.status for s in seasons} == {RequestStatus.cancelled}


async def test_cancel_rejects_an_already_imported_request(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.available,  # already imported -> not cancellable
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(
                session, FakeQbittorrent(), request_id=request_id
            )


async def test_cancelled_request_no_longer_blocks_a_fresh_request(
    sessionmaker_: SessionMaker,
) -> None:
    # A cancelled row is SETTLED (outside uq_media_requests_active): a brand-new
    # active request for the same media must be insertable alongside it.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        await correction_service.cancel_request(session, FakeQbittorrent(), request_id=request_id)

    async with sessionmaker_() as session:
        fresh = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.pending,
        )
        session.add(fresh)
        await session.commit()  # must NOT raise IntegrityError
        assert fresh.id != request_id
