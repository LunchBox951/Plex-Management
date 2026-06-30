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
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.services import queue_service
from tests.web.fakes import FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "f" * 40
_TITLE = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_INDEXER = "FakeIndexer"


async def _seed_request_with_download(
    sm: SessionMaker, *, first_seen_at: datetime, indexer: str | None = _INDEXER
) -> int:
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
                source_title=_TITLE,
                indexer=indexer,
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


async def test_auto_fail_blocklist_records_indexer_and_blocks_hashless_candidate(
    sessionmaker_: SessionMaker,
) -> None:
    """The auto-fail blocklist row carries the originating indexer (recovered from
    history), so a later candidate from that indexer with NO info_hash is rejected
    by the pure tier-2 (title + indexer) check — blocklist-then-research holds for
    hashless feeds."""
    await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        entry = (await session.execute(select(Blocklist))).scalar_one()
        assert entry.indexer == _INDEXER

        repo = SqlBlocklistRepository(session)
        # A re-searched candidate that exposes NO info_hash (only title+indexer) is
        # still rejected via tier 2 — the bug was an indexer=None blocklist row.
        blocked = await repo.is_blocklisted(
            tmdb_id=603, torrent_hash=None, source_title=_TITLE, indexer=_INDEXER
        )
        assert blocked is True
        # A different indexer with the same title is NOT blocked (tier-2 is scoped).
        other = await repo.is_blocklisted(
            tmdb_id=603, torrent_hash=None, source_title=_TITLE, indexer="OtherIndexer"
        )
        assert other is False


async def test_live_progress_persisted_without_state_change(
    sessionmaker_: SessionMaker,
) -> None:
    """A download advancing 10%->50% while staying 'Downloading' emits NO state
    transition from the pure reconciler, but reconcile_and_list must still persist
    the live progress/seed_ratio — otherwise the queue shows stale progress."""
    async with sessionmaker_() as session:
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            tmdb_id=603,
            progress=0.1,
            seed_ratio=0.0,
        )
        session.add(download)
        await session.commit()

    # The client reports the SAME mapped state ('downloading') but further along.
    live = DownloadStatus(
        info_hash=_HASH,
        name="Some.Movie",
        raw_state="downloading",
        progress=0.5,
        ratio=1.2,
    )
    async with sessionmaker_() as session:
        queue = await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[live]), session)

    item = next(i for i in queue if i.torrent_hash == _HASH)
    assert item.status == "downloading"  # unchanged state
    assert item.progress == 0.5  # progress moved despite no transition
    assert item.seed_ratio == 1.2

    async with sessionmaker_() as session:
        persisted = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert persisted.progress == 0.5
    assert persisted.seed_ratio == 1.2


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


async def test_mark_failed_without_blocklist_rearms_request(
    sessionmaker_: SessionMaker,
) -> None:
    """mark_failed(blocklist=False) must still reconcile the owning request: the
    download goes terminal Failed, so the request cannot stay 'downloading' with no
    active download (a dishonest state). The blocklist flag gates ONLY whether a
    Blocklist row is written, not the request re-arm."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            media_request_id=request.id,
            tmdb_id=603,
        )
        session.add(download)
        await session.commit()
        request_id, download_id = request.id, download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(session, download_id=download_id, blocklist=False)
    assert record.status == "failed"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching  # re-armed despite blocklist=False
    assert blocklist == []  # but no blocklist row was written
