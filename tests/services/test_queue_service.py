"""queue_service — the auto-fail blocklist-and-research path beyond grace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
)
from plex_manager.services import queue_service
from tests.web.fakes import FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "f" * 40


async def _seed_request_with_download(sm: SessionMaker, *, first_seen_at: datetime) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=603,
                first_seen_at=first_seen_at,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=603,
                torrent_hash=_HASH,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
            )
        )
        await session.commit()
        return request.id


async def test_missing_beyond_grace_fails_blocklists_and_researches(
    sessionmaker_: SessionMaker,
) -> None:
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    # The client reports nothing — the torrent is gone beyond the grace window.
    async with sessionmaker_() as session:
        queue = await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    # The blocklist + re-search fired, so the download completed FailedPending ->
    # Failed and drops out of the active queue (no zombie row left behind).
    statuses = {item.torrent_hash: item.status for item in queue}
    assert _HASH not in statuses

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        request = await session.get(MediaRequest, request_id)
        failed = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()

    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _HASH
    # The blocklist entry carries the real grabbed title (from history), not the hash.
    assert blocklist[0].source_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
    assert request is not None
    assert request.status is RequestStatus.searching
    # The row reached the terminal Failed state (not stranded at failed_pending).
    assert failed.status == "failed"


async def test_mark_failed_routes_downloading_through_failed_pending(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        download = Download(torrent_hash=_HASH, status="downloading", tmdb_id=603)
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(session, download_id=download_id, blocklist=False)
    assert record.status == "failed"
